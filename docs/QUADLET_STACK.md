# Quadlet Stack

The `quadlet_stack` role deploys every app of a host, each one with
[`quadlet_app`](QUADLET_APP.md). It adds two things a single app's deploy cannot do:

- **It creates every app's networks first**, so apps deploy in any order, and one app can be
  deployed on a host where the app owning its network never was. Networks are the only resource
  apps share - see [Apps are independent](#apps-are-independent).
- **It satisfies the resources apps claim.** An app declares what it needs from outside itself - a
  route, a scrape, a DNS record - in a manifest beside its quadlets. The role hands each claim to
  the role named to satisfy it, before and after the deploy. Nothing outside the app is edited to
  add one.

```
quadlet-apps/
├── web/
│   ├── application.yml    # the manifest: what the app claims
│   ├── quadlets/
│   └── config/
└── ingress/
    ├── quadlets/
    └── config/
```

## Usage

```yaml
- name: Deploy the apps
  ansible.builtin.import_role:
    name: kriansa.quadlet.quadlet_stack
  vars:
    quadlet_stack_deploy_only: "{{ [app] if app is defined else [] }}"
    quadlet_stack_state: started
  tags: [deploy]
```

| Variable | Default | |
|---|---|---|
| `quadlet_stack_src` | `quadlet-apps` | The directory holding one directory per app, relative to the playbook |
| `quadlet_stack_deploy_only` | `[]` | The apps to deploy, by name. Empty deploys every app |
| `quadlet_stack_state` | `installed` | `installed`, `started` or `restarted`, as for `quadlet_app` |

Import it with `import_role`. The tags and keywords such as `become` given to the import reach every
task of the role, and through them everything the role includes - provider roles and their handlers
too. The role sets no tags of its own, so any tag works.

`include_role` works as well, with `apply:` repeating the tags and keywords: on an include they
reach the include task alone, and `apply` is what gives them to the role's tasks. Use it only to
run the role under a `when:` decided once. An import copies its `when:` onto every task of the role
and checks it before each one, so a condition the role or a provider changes would stop the deploy
partway.

Every directory under `quadlet_stack_src` whose name does not start with `.` is an app. It must have
a `quadlets/` directory and a lowercase name, since it is deployed under that name. Creating a
directory there is enough to deploy it.

## What a run does

```
collect   →  read every app, host-wide: manifests, claims, the networks each app joins
networks  →  create every app's networks that are missing - every app, not only the selected
before    →  run every provider named by a claim, with quadlet_hook: before
deploy    →  deploy the selected apps, one at a time
after     →  run every provider again, with quadlet_hook: after
```

Each host deploys its apps one at a time, in name order when every host deploys the same apps.
With several hosts deploying different sets, Ansible runs the includes they have in common
together, so one host can deploy an app before another that comes first by name. Apps don't rely on
any order - see [Apps are independent](#apps-are-independent).

- **Collect reads every app, whichever are deployed.** A provider has to see every claim on the host:
  one that removes what nothing claims, shown only the apps deployed this run, would remove what
  the others claim. So one app that cannot be read - a broken manifest, a template error, a secret
  its quadlets look up that is not available - fails every run, even one deploying another app.
- **Selection is only selection.** `quadlet_stack_deploy_only: [web]` deploys web and restarts
  nothing else. A name that is not an app fails the run before anything is done.
- **The deploy stops at the first app that fails**, and says which apps were deployed before it.
  The `after` pass does not run.

### Apps are independent

**An app must tolerate the apps it talks to being absent at start, and recover on its own.** The
role never orders apps against each other. Handling another service being down is the problem an
app already has when that service restarts or crashes, and every app starts at once on boot anyway.

Networks are the one exception, and are why the `networks` step exists. A container whose network
does not exist is never created - podman refuses it outright, and systemd gives up retrying long
before the network turns up. So the role creates every app's networks first, rather than ordering
the apps that join them.

That step writes the network files missing from the host, and starts the networks that are not
running - writing the current file first for one of those, which nothing can be using, so a network
that failed to start is retried with its fixed definition. A network file already there and running
is left alone even if its app changed it: that change is its own app's to deploy. Creating a network
never disturbs one in use.

A network podman does not have - removed by hand, or renamed by a `NetworkName=` change its app
deployed - is created again, whatever `quadlet_stack_state` is, restarting every container joined
to it, other apps' included. So a rename deployed under `installed` is applied by the next stack
run, under `installed` too.

Networks are the only resource apps share. An app relies on its own volumes and pods alone: naming
another app's, as in `Volume=media--files.volume` or `Pod=media--edge.pod`, is not supported, and
on a host where that app is not deployed yet podman's generator rejects it.

## The manifest

`application.yml` (or `application.yaml` - not both) beside the app's `quadlets/`. An app without
one gets the defaults, and claims nothing.

```yaml
version: "v1"

# Optional. Seconds Ansible waits for each systemctl command. Defaults to 300.
systemctl_timeout: 900

# Optional. The quadlet whose service is the app's main one, without extension. Defaults to "main".
main_container: main

# Optional. What the app needs from outside itself, by the role that provides it.
resource_claims:
  kriansa.commons.quadlet_provider.example_dns:
    - name: {{ (quadlet_app_name ~ '.' ~ public_dns_search) | to_json }}
      value: {{ public_ip | to_json }}
  caddy-ingress:
    - domain: {{ (quadlet_app_name ~ '.' ~ public_dns_search) | to_json }}
      upstream: {{ (quadlet_app_name ~ '--main:8080') | to_json }}
```

See [quadlet_app's manifest section](QUADLET_APP.md#manifest) for what `systemctl_timeout` does and
does not bound.

### Version

`version` is required, and is a **string**: YAML reads `1` as a number and `1.0` as a fraction.
Everything before the first `/` is the version the file is read as; the rest is a variant tag, so
`v1/testing-new-thing` reads as `v1`. A version this collection does not know fails the run rather
than risk misreading the file.

### Unknown keys

A key the version does not define gets a warning, and is otherwise ignored - so a misspelled
`main_contaner` is visible but not fatal. Keys starting with `x_` or `_` are yours, and are ignored
without a word.

### Templating

The manifest is templated with everything the play sees - variables, lookups, filters, `{% %}`
blocks - and `quadlet_app_name`, exactly like the app's other files. It is **rendered, then read as
YAML**. It never reaches the managed host.

It is rendered **once**, by the collect step. The deploy uses that result rather than rendering the
file again with other variables in scope, so the values the providers were given are the ones the
app is deployed with.

**Template values as `{{ var | to_json }}`, unquoted.** Rendering happens before YAML reads the
file, so YAML reads whatever a value renders to - the braces are long gone by then:

| Written as | `var` holds | YAML reads |
|---|---|---|
| `key: {{ var }}` | `yes` | **`True`** - a boolean, silently |
| `key: {{ var }}` | `1.0` | **`1.0`** - a number, silently |
| `key: {{ var }}` | text with `: `, or starting with `*` or `%` | the run fails |
| `key: "{{ var }}"` | `900`, for `systemctl_timeout` | **`"900"`** - text, so the run fails |
| `key: "{{ var }}"` | `C:\temp\new` | **a tab and a newline** in place of `\t` and `\n`, silently |
| `key: {{ var \| to_json }}` | any of these | ✅ that same value |

JSON is YAML, and `to_json` escapes whatever needs it, so the value reads back as the type and text
it was: a number stays a number, a string with quotes, backslashes or newlines stays that string,
and a list stays a list. When the file does not parse, the error shows the rendered text, which is
what broke; the file on disk looks fine.

A literal value, not templated, is written as in any YAML file. Single-quoted scalars process no
escapes, which suits regular expressions: `\d` survives.

**Manifests are not a place for secrets.** Claims are part of what the role registers and shows with
`-v`, and a parse error prints the rendered file.

## Resource claims

A claim is addressed to **the role that satisfies it**, by the name `include_role` would take:

| Key | Resolves to |
|---|---|
| `kriansa.commons.quadlet_provider.example_dns` | `roles/quadlet_provider/example_dns/` in that collection |
| `caddy-ingress` | a role on the roles path |
| `geerlingguy.docker` | a role installed from Galaxy, on the roles path |

A dotted name is either a collection's role - `namespace.collection.role`, with subdirectories of
`roles/` in between - or a directory on the roles path with dots in its name, as Galaxy names the
roles it installs. Letters, digits, `_`, `-` and `.` between them are allowed. A key that is not a
role name fails the run, naming the manifest, and so does a role named twice in one manifest. A
valid name of a role that does not exist fails the `before` pass, before any app is deployed - the
message names the role but not the app claiming it.

What a claim holds is never checked here. Its shape is for the role receiving it to judge.

There is no registry mapping a kind of claim to an implementation: an app names the role. Swapping
one provider for another means editing every manifest that claims it.

## Providers

A provider is an ordinary Ansible role. It is included **twice per run**, before and after the
deploy, with all the claims addressed to it - from every app, deployed this run or not:

| Variable | |
|---|---|
| `quadlet_hook` | `before` or `after`: which side of the deploy this is |
| `quadlet_claims` | every claim addressed to this role, as `{app, claim}`, in app order then manifest order |
| `quadlet_stack` | everything the collect step read - see [What a provider is given](#what-a-provider-is-given) |

```yaml
# roles/example_dns/tasks/main.yml
- name: DNS provider
  when: quadlet_hook == 'before'
  block:
    - name: Create DNS records
      some.collection.dns_record:
        host: "{{ item.claim.name }}"
        address: "{{ item.claim.value }}"
      loop: "{{ quadlet_claims }}"
      loop_control:
        label: "{{ item.claim.name }} → {{ item.claim.value }}"
```

- **Guard the whole role with one `block:`.** Every provider is included on both hooks. One task
  missing its `when:` runs twice per deploy, the first time against the host as it was before.
- **Test `quadlet_hook` for equality.** `quadlet_hook != 'after'` also matches any hook added later.
- A provider's handlers run as they would in any role, and do even when the run fails if the
  playbook runs with `--force-handlers`.

### Writing configuration for another app

A provider that produces configuration should write a **separate file** the consuming service
includes - most services support an include glob or a config directory - rather than edit one.
Use the consumer's `config_dir` rather than a path of your own:

```yaml
- name: Ingress provider
  when: quadlet_hook == 'after'
  block:
    # The consumer may not be on this host yet: a run that deploys only another app still gives
    # every provider every claim
    - name: Check the ingress is deployed
      ansible.builtin.stat:
        path: "{{ quadlet_stack.apps.ingress.config_dir.main }}"
      register: ingress_config

    - name: Render the generated routes
      ansible.builtin.template:
        src: routes.conf.j2
        dest: "{{ quadlet_stack.apps.ingress.config_dir.main }}/conf.d/generated.conf"
        mode: "0644"
        # What the container may read on an SELinux host
        setype: container_file_t
      when: ingress_config.stat.exists
      notify: reload ingress
```

Check rather than assume:

- **Is the consumer deployed?** `config_dir` says where its files go, not that they are there: a
  run deploying only another app still runs every provider. Skip it, as above, or create the
  directory.
- **Is the file labelled for the container?** On an SELinux host a new file gets the label its path
  defaults to, which a container cannot read; the `Z` the module mounts config with only relabels
  when the container starts. Set `setype: container_file_t`, or restart the consumer rather than
  reload it. Leave the level out: `Z` gives the files the container's own level each time it
  starts, and a `selevel` would set it back - reporting `changed`, and reloading the consumer for
  nothing, on every run after a restart.
- **Does the service have a reload command?** Restarting it instead also restarts every unit that
  `Requires=` it: the consumer's containers, and any other app's.
- **Stage, validate, then move.** Validate the merged configuration before it is in place: a
  validator run on a live file cannot stop a later restart from loading a broken one.
- **Duplicate definitions usually break the whole configuration.** Detect a generated entry
  colliding with a hand-written one in the `before` hook, and fail there.
- **The directory must exist before the consumer starts.** On a fresh host the consumer deploys
  before the provider writes to it, so ship a placeholder in the consumer's `config/`.

Write where each entry came from into the file (`# declared by web`): the configuration of one
service is now in two files.

### Removing what is no longer claimed

There is no state kept between runs. A provider that has to remove what nothing claims any more
does it itself: **mark what it creates, list what carries the mark, remove what nothing claims.**
Scope the mark to the host when several hosts share one system, or one host's run removes another's.

The same goes for generated files. One file rewritten whole converges on its own. One file per app
does not: `quadlet_app` never removes files, so a dropped claim's file would stay. Either write one
file, or list and remove the stale ones.

A provider can also only ever add, and leave removals to a person - say so in its documentation.

## What a provider is given

`quadlet_stack` is what [`quadlet_stack_info`](../plugins/modules/quadlet_stack_info.py) returns:

```yaml
quadlet_stack:
  src: /home/me/playbooks/quadlet-apps
  apps_list: [app_one, consumer_app]
  apps:
    app_one:
      version: v1                 # null when the app has no manifest
      systemctl_timeout: 900
      main_container: main
      quadlets: [db, main]        # its containers, pods and kube files, by name
      networks: [app_one--main, consumer_app--main]
      config_dir:
        main: /srv/app_one/config/main
  claims_by_role:
    kriansa.commons.quadlet_provider.example_dns:
      - { app: app_one, claim: { name: app-one.example, value: 10.0.0.1 } }
```

- **`networks`** are the networks the app's containers, pods and kube files join, by the name podman
  knows them by - `Network=main.network` in app_one reads as `app_one--main`, and a network's own
  `NetworkName=` is honoured. Modes that join no network (`host`, `none`, `container:…`…) are left
  out, and an empty `Network=` clears the ones before it, as it does for podman. Only networks
  named in a `Network=` are listed: podman's default network, which a container without one or
  with `Network=bridge` joins, is not - every such container shares it, so it says nothing of which
  apps meant to reach each other. A network file no
  app of the stack defines is named by guess: `<app>--<name>` as quadlet_app names it, or
  `systemd-<name>` as quadlet names one of the host's own. It is the one thing a provider cannot
  work out for itself, and lets it check a claim without this collection knowing what the claim
  means:

  ```yaml
  - name: Every scrape target must be reachable from the agent
    ansible.builtin.assert:
      that: "'agent--main' in quadlet_stack.apps[item.app].networks or item.claim.address is defined"
      fail_msg: "{{ item.app }} claims a scrape but joins neither agent--main nor sets an address"
    loop: "{{ quadlet_claims }}"
  ```

- **`config_dir`** is where each of the app's `config/<quadlet>/` directories is deployed on the
  host, keyed by quadlet. Only directories holding a file are listed: addressing one the app lacks
  fails as an undefined attribute. It is where the files go, not a sign they are there - the app
  may not be deployed on this host yet. Use it rather than building the path, which may change.

## Validation

The collection checks the manifest's form only: that it parses, with no key repeated, that the
version is known, that `resource_claims` maps role names to lists, and that every app has its main
quadlet.

| Failure | Caught by |
|---|---|
| A claim's own content | the provider receiving it |
| A resource that is not actually reachable | the consuming service |
| Generated configuration that is wrong | the provider's validate step |
| A claim key that is not a role name, or a key repeated | the collect step, naming the manifest |
| A role name that resolves to nothing | the `before` pass, before any app deploys |
| A container not linked to its app's main one | a warning from the deploy - see [quadlet_app](QUADLET_APP.md#dependency-handling) |

## Reading the stack yourself

The role is built on `kriansa.quadlet.quadlet_stack_info`, which runs on the control node only and
can be used on its own:

```yaml
- name: Read the apps
  kriansa.quadlet.quadlet_stack_info:
    src: quadlet-apps
  register: stack
```
