# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""Bringing the app's services and timers to the state the task asks for."""

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFiles
from ansible_collections.kriansa.quadlet.plugins.module_utils.systemctl import ServiceWatch, Systemd


class AppServices:
    """
    The app's services and timers on the host, and what a run has to do to them.

    Built before anything is deployed: some of what it decides depends on the host as it was
    before this run wrote to it.
    """

    def __init__(
        self, app: QuadletApp, systemd: Systemd, files: HostFiles, state: str, settle_time: int
    ):
        """
        Args:
            app: The app being deployed
            systemd: Where to run and ask about units
            files: The app's files, before any is written
            state: installed, started or restarted
            settle_time: Seconds the services a run starts have to stay up; 0 to not check
        """
        self.app = app
        self.systemd = systemd
        self.state = state
        self.settle_time = settle_time
        # Deployed timer name -> whether its file content changes this run. Recorded before
        # deployment overwrites the files, so the timers can still be told apart afterwards.
        self.timer_changes = {timer.deployed_name: files.changed(timer) for timer in app.timers}
        # Generated ownership bootstrap services whose volume has yet to get its declared owner
        self.pending_volume_inits = app.pending_volume_inits()

    def need_attention(self) -> bool:
        """Whether the services are not in the state asked for, whether or not any file changes."""
        # Checked before the state branches: a timer someone stopped or disabled by hand has to
        # bring the module past its early exit, whatever state the main service is in
        if self._has_stale_timer():
            return True

        if self.state == "started":
            # Need to start if not currently active, or to give a volume its declared owner
            return not self.systemd.is_active(self.app.main_service) or bool(
                self.pending_volume_inits
            )
        if self.state == "restarted":
            # Always restart when restarted state is requested
            return True

        return False

    def apply(self, files_changed: bool) -> bool:
        """
        Reload systemd, then enable, start and restart what the state asks for.

        Args:
            files_changed: Whether this run wrote any file

        Returns:
            True if anything was done
        """
        changed = False
        main_service = self.app.main_service

        # Reload daemon if files changed
        if files_changed:
            self.systemd.run("daemon-reload")
            changed = True

        # Timers are reconciled on every state, main service or not, and only after the reload
        # above - systemd has not seen a newly written unit file before that
        if self._reconcile_timers():
            changed = True

        # Which run of each service was current before anything is started, so the ones this run
        # starts - itself, or through systemd pulling them in - can be told apart afterwards
        watched = []
        if self.state != "installed" and self.settle_time > 0:
            watched = self.app.services()
        watch = ServiceWatch(self.systemd, watched, self.settle_time)

        # Determine if we need to restart dependencies and main service
        needs_restart = False
        if self.state == "restarted":
            # Always restart when restarted state is requested
            needs_restart = True
        elif self.state == "started" and files_changed:
            # Restart when files changed, whether main runs or not: a container main depends on may
            # still be running its old definition, and starting main alone would leave it so.
            # Restarting a stopped unit starts it.
            needs_restart = True

        if needs_restart:
            self._restart_app()
            changed = True
        else:
            # Main is not being restarted, but a volume waiting for its owner still gets it - and
            # before main mounts the volume, if it is about to be started below. Not under
            # installed, which leaves running containers alone: running a bootstrap unit again
            # restarts every container that mounts its volume.
            if self.state != "installed" and self._restart_volume_init_units():
                changed = True
            if self.state == "started" and not self.systemd.is_active(main_service):
                # Start service if not already active (no restart needed)
                self.systemd.run("start", main_service)
                changed = True

        watch.check()

        return changed

    def _restart_app(self) -> None:
        """
        Restart the main service, every app unit it depends on, and the pending bootstrap units.

        All of them go to systemd in one call, so it builds a single transaction: each unit stops
        and starts once, in dependency order, and a bootstrap unit finishes before the containers
        mounting its volume start. Split into separate calls, a unit that requires one restarted
        earlier is restarted again by systemd on its own - which is how main used to go round two
        or three times per deploy.

        The bootstrap units main depends on whose owner is already applied are left out.
        Restarting one does nothing useful - its marker makes the condition fail and the unit is
        skipped - and it would still take down every container that requires it.
        """
        main_service = self.app.main_service
        all_init_units = set(self.app.volume_init_services())
        dependencies = [
            unit
            for unit in self.systemd.dependencies(main_service, f"{self.app.name}--")
            if unit not in all_init_units
        ]
        units = self.pending_volume_inits + dependencies + [main_service]
        self.systemd.run("restart", *dict.fromkeys(units))

    def _reconcile_timers(self) -> bool:
        """
        Bring every deployed timer to enabled and running.

        This is the one place the module runs `systemctl enable`, and the reason is that a timer is
        not a quadlet. Podman's generator applies the [Install] section of the units it generates
        itself - they are transient and cannot be enabled at all - so the module never has to. A
        timer is an ordinary unit file that systemd will not arm unless somebody enables it, and
        nobody else is going to.

        Returns:
            True if any timer was touched
        """
        changed = False

        for timer in sorted(self.timer_changes):
            if not self.systemd.is_enabled(timer):
                self.systemd.run("enable", timer)
                changed = True

            # Covers both reasons to run it: it is not running at all, or its schedule moved and
            # systemd is still holding the elapse computed from the old one. Enabling alone would
            # not do it - timers.target was reached long before this runs, so a newly enabled
            # timer sits dead until the next boot.
            if self.timer_changes[timer] or not self.systemd.is_active(timer):
                self.systemd.run("restart", timer)
                changed = True

        return changed

    def _restart_volume_init_units(self) -> bool:
        """
        Run the pending ownership bootstrap units.

        They are Type=oneshot with RemainAfterExit=yes, so once one has run it stays active and
        systemd never re-checks its condition. Without an explicit restart, a new owner would not
        be applied until the host next booted.

        These cannot be picked up by the dependency walk in _restart_app: that starts from the main
        service, and a volume mounted only by a secondary container is not in its tree.

        Returns:
            True if any unit was run
        """
        if not self.pending_volume_inits:
            return False
        self.systemd.run("restart", *self.pending_volume_inits)
        return True

    def _has_stale_timer(self) -> bool:
        """Whether any deployed timer is not enabled or not running."""
        return any(
            not self.systemd.is_enabled(timer) or not self.systemd.is_active(timer)
            for timer in self.timer_changes
        )
