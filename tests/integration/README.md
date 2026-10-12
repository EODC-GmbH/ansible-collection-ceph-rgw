# Integration: converge twice against a live cluster

The unit tests in `tests/unit` run every module against an in-memory cluster whose answers are
checked against a transcript recorded on a live cluster of each release (see `capture_rgw.sh`
below). A call the transcript does not cover is still a guess, so this check runs the real roles
against a real cluster.

`converge_twice.sh` builds this checkout, puts it first on `ANSIBLE_COLLECTIONS_PATH` and checks
that Ansible loads it instead of another installed copy. It then runs `converge.yml` (rgw_sts,
rgw_tenants, rgw_iam, in that order) four times:

| Step | Flags | Must |
|---|---|---|
| preview | `--check --diff` | succeed; lists the tasks the converge will change |
| converge | | succeed |
| second | | succeed with `changed=0` on every host |
| recheck | `--check` | report `changed=0` |

On failure it prints the tasks that changed. Logs stay in the scratch directory it prints.

The converge step changes the cluster, so use a test cluster. `--check-only` stops after the
preview.

## The test-cluster file

The live scripts (`converge_twice.sh`, `capture_rgw.sh`, `scenarios.sh`) take their cluster from
one file and ignore any `RGW_TEST_*` set in the shell, so a leftover export cannot send a run to
another cluster. Copy `test-cluster.env.example` to `~/.config/ceph_rgw-tests/test-cluster.env`
(or pass `-c <file>`) and fill it in:

| Variable | Used by | |
|---|---|---|
| `RGW_TEST_FSID` | all | fsid of the test cluster; required |
| `RGW_TEST_RUNNER` | all | the command that runs a playbook, as a bash array; required |
| `RGW_TEST_KAYOBE_ENVIRONMENT` | all | the run stops unless `KAYOBE_ENVIRONMENT` matches |
| `RGW_TEST_SSH`, `RGW_TEST_TENANTS_DIR` and the rest | `scenarios.sh` | see the example |

Before anything changes, `fsid.yml` runs through the runner and every host it reaches must name
`RGW_TEST_FSID` in `/etc/ceph/ceph.conf`. `scenarios.sh` checks `RGW_TEST_SSH` the same way,
because it injects drift there. A mismatch stops the run with exit code 2.

## Running it

The runner gets the playbook path and the flags appended. With kayobe, use
`RGW_TEST_RUNNER=(kayobe playbook run)` from the activated test environment:

```sh
tests/integration/converge_twice.sh --check-only
tests/integration/converge_twice.sh
tests/integration/converge_twice.sh -c ~/hooli-test.env   # another test-cluster file
```

With plain Ansible, `RGW_TEST_RUNNER=(ansible-playbook -i inventory -e @rgw.yml)`. The plays
target the `mons` group; add `-e rgw_converge_hosts=<pattern>` to the runner for another.

To exercise the repair path, introduce drift first (edit a role policy, change a quota, delete a
bucket) and expect the converge step to report it and the second run to be clean. Key drift is
not repaired: a user whose cluster keys differ from the vaulted pair, or who holds no key at all,
stops the run until `rotate_keys` is set, so a key revoked by hand is never put back silently.

# Integration: recording a release

`capture_rgw.sh` reads the same test-cluster file and runs `capture.yml`. It creates the account
`RGW90000000000000001`, runs the radosgw-admin, ceph, S3 and IAM calls the modules make, and
removes the account again. It refuses an account that already exists. Generated keys are replaced
by placeholders, and the transcript is written to `tests/unit/fixtures/<release>.json`.

```sh
tests/integration/capture_rgw.sh
```

`tests/unit/ceph_releases.py` holds each release's answers for the fakes.
`tests/unit/test_fake_matches_capture.py` replays the transcript against them and compares exit
codes, the first stderr line, and the keys and types of read output. Answers the capture does not
provoke are listed under `assumed`. A release without a capture waits in `PENDING`, and its tests
are skipped. To add tentacle, run the capture on a tentacle cluster, add its profile, and move it
from `PENDING` to `RELEASES`.

# Integration: converge in memory

`inmemory.sh` runs `converge_twice.sh` with `ansible-playbook` against an in-memory cluster on
this machine, through a test-cluster file it writes for each case (fsid from
`inmemory/ceph.conf`). `inmemory/bin` puts fake `radosgw-admin` and `ceph` executables first on
`PATH`, and `inmemory/endpoint.py` serves S3 and IAM on 127.0.0.1. Both answer from one state file
as the release in `FAKE_CEPH_RELEASE` (default `squid`). The site is `inmemory/vars.yml`: two
tenants and one service user.

| Case | Starts from |
|---|---|
| sts-enabled | `rgw_sts_key` and `rgw_s3_auth_use_sts` already set |
| greenfield | an empty cluster |

```sh
FAKE_CEPH_PYTHON=/path/to/python3 tests/integration/inmemory.sh
FAKE_CEPH_PYTHON=/path/to/python3 tests/integration/inmemory.sh greenfield
```

`FAKE_CEPH_PYTHON` runs the modules and the endpoint, and needs ansible-core and boto3. The
endpoint answers 501 to an S3 or IAM call it does not model, and the fake executables exit 70 for
a command they do not model.

# Integration: lifecycle scenarios

`scenarios.sh` reads the same test-cluster file and walks one throwaway tenant (default `citest`)
through the whole lifecycle, asserting exit codes, changed counts and messages. Drift is injected
with `radosgw-admin` and `ceph` directly, not with the code under test. Every converge is limited
to the test tenant with `rgw_tenants_limit`, so a run costs the same however many tenants the
cluster has; `converge_twice.sh` covers the whole estate. Steps run in order and the first failure
stops the run:

| Step | What it proves |
|---|---|
| reset | any leftover test account is purged (including its OIDC provider) |
| generate | `rgw_new_tenant` / `rgw_add_root_user` / `rgw_add_service_user` refuse bad input and write nothing; re-adding is a no-op |
| preview | `--check --diff` of a new tenant succeeds, lists WOULD lines, creates nothing; `rgw_tenants_limit` naming an unknown tenant, or given as a string, refuses |
| create | converge creates it; the second run is clean; roles, trust grants and prefix scope are right |
| drift | a changed policy, trust, quota, a deleted bucket are repaired; prune removes an unmanaged role and keeps `<tenant>-x-*` |
| nokey | a user with no key refuses (also under `--check`) until `rgw_tenants_rotate_keys` |
| mismatch | a root user holding a different key refuses until `rgw_tenants_rotate_keys`, which leaves exactly the vaulted key |
| keys | extra `access_keys` on the tenant root and a service user are created with their secrets; a changed secret refuses (also `--check`) until `rgw_tenants_rotate_keys`, which also removes undeclared keys; without rotation an undeclared key stays; `absent` removes a key; declaring the primary `absent` refuses |
| identity | a changed display name refuses in check mode and in a real run, and changes nothing |
| oidc | a deleted OIDC provider is recreated with no RGW restart |
| sts | STS auth switched off is switched back on, and the handler waits for the RGW restart |
| readonly | the report, discover and verify modes change nothing on the cluster |
| federation | throwaway IdP users assume reader (read-only), writer and owner, are kept out of another tenant's bucket and out of roles their groups do not grant; the prefix role reads under its prefix only; a removed provider refuses assumes and works again right after the repair, with no restart |
| teardown | `state: absent` is skipped with a warning without the gate, refuses with mismatched keys, then removes everything; re-runs, and a whole-estate run without the drop-in, are clean |

```sh
tests/integration/scenarios.sh
tests/integration/scenarios.sh -s drift,nokey   # resume at chosen steps
tests/integration/scenarios.sh --keep           # leave the tenant up
```

The real drop-ins are only read: the run passes `-e rgw_tenants_dir` pointing at a scratch
directory of symlinks to them plus the test tenant, kept under `~/.cache/ceph_rgw-scenarios`. The
sts step restarts RGW once. The federation step is skipped unless `RGW_TEST_IDP` and
`RGW_TEST_KC_ADMIN` are set. `RGW_TEST_KC_ADMIN` is your own command that creates and removes the
throwaway IdP users (`user <name> <password> <group-path>...`, `rm-user <name>`,
`rm-group <group-path>`); the step removes them and the probe objects it seeds, also when it
fails. It also needs `RGW_TEST_CLIENT`, the OIDC client id. `test-cluster.env.example` lists every
setting.
