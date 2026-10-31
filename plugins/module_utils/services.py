# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""Bringing the app's services and timers to the state the task asks for."""

import functools

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFiles
from ansible_collections.kriansa.quadlet.plugins.module_utils.podman import Podman
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
        # What the task should be warned about, found while applying
        self.warnings: list[str] = []

    def need_attention(self) -> bool:
        """Whether the services are not in the state asked for, whether or not any file changes."""
        # Checked before the state branches: a timer someone stopped or disabled by hand has to
        # bring the module past its early exit, whatever state the main service is in
        if self._has_stale_timer():
            return True

        if self.state == "started":
            # Need to start if not currently active, to give a volume its declared owner, or to
            # create a network podman does not have - renamed by a deploy under installed, which
            # leaves running units alone, or removed by hand
            return (
                not self.systemd.is_active(self.app.main_service)
                or bool(self.pending_volume_inits)
                or bool(self._missing_networks)
            )
        # Always restart when restarted state is requested
        return self.state == "restarted"

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
            # Asked only now that systemd has the new units: a dependency added this run counts
            self.warnings.extend(self._loose_service_warnings())

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
        elif self.state == "started":
            # Main is not being restarted, but some units it depends on still have to run again:
            # a volume waiting for its owner gets it - before main mounts the volume, if it is about
            # to be started below - and a network podman does not have is created again. Both
            # stay active after running once, so main requiring them would not run them again.
            # Not under installed, which leaves running containers alone: restarting either
            # restarts every container that uses it.
            rerun = self.pending_volume_inits + self._missing_networks
            if rerun:
                # In one call, so a container using both - main among them - restarts only once
                self.systemd.run("restart", *rerun)
                changed = True
            if not self.systemd.is_active(main_service):
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

        Only the app's own units are restarted. Main's dependencies include whatever else it
        requires - host units, or another app's network - and those are not this app's to
        restart. Units are told apart by name, not by the app's prefix, which another app's
        name can start with too.

        The bootstrap units main depends on whose owner is already applied are left out.
        Restarting one does nothing useful - its marker makes the condition fail and the unit is
        skipped - and it would still take down every container that requires it.

        So are the app's networks, unless podman no longer has them. systemd restarts every unit
        that requires a restarted one, and the containers of other apps joining the network
        require it - restarting it on every deploy would restart them too. And it would apply no
        change: podman creates a network only if it does not exist. One that does not exist -
        renamed by a NetworkName= change, or removed by hand - is restarted, so podman creates it:
        its unit stays active after running once, and main requiring it would not run it again.
        The apps joined to it are restarted with it, which a network that changed name needs.
        """
        main_service = self.app.main_service
        own = self.app.own_services()
        left_out = set(self.app.volume_init_services()) | {
            network.service_name for network in self.app.networks
        }
        dependencies = [
            unit for unit in self._main_dependencies or [] if unit in own and unit not in left_out
        ]
        units = self.pending_volume_inits + self._missing_networks + dependencies + [main_service]
        self.systemd.run("restart", *dict.fromkeys(units))

    @functools.cached_property
    def _main_dependencies(self) -> list[str] | None:
        """
        Main's dependencies, as systemctl reports them; None if it could not say.

        Asked once: everything that asks runs after the reload that makes systemd see this run's
        files, and nothing in between changes the units.
        """
        return self.systemd.dependencies(self.app.main_service)

    @functools.cached_property
    def _missing_networks(self) -> list[str]:
        """
        The services of the app's networks that podman does not have.

        Asked once: nothing a run does before restarting them creates or removes a network.
        """
        return [
            network.service_name
            for network in self.app.networks
            if not Podman.network_exists(network.network_name)
        ]

    def _loose_service_warnings(self) -> list[str]:
        """
        A warning for each of the app's services that nothing links to its main one.

        The module starts and restarts main and what main depends on, and leaves the rest of the
        orchestration to systemd. A container main does not require or want, directly or not, is
        never started by a deploy, nor restarted when it changes. The services the app's timers
        run are left out: they are meant to run on their schedule only.
        """
        main_service = self.app.main_service
        tree = self._main_dependencies
        if tree is None:
            # systemd could not say; better no warning than one about every service
            return []

        linked = set(tree) | {main_service} | self.app.timer_targets()
        return [
            f"{service} is not linked to {main_service}, so deploying the app never starts it, "
            "nor restarts it when it changes. Add Requires= or Wants= on it to the main quadlet's "
            "[Unit] section, or have a timer run it."
            for service in self.app.services()
            if service not in linked
        ]

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

    def _has_stale_timer(self) -> bool:
        """Whether any deployed timer is not enabled or not running."""
        return any(
            not self.systemd.is_enabled(timer) or not self.systemd.is_active(timer)
            for timer in self.timer_changes
        )
