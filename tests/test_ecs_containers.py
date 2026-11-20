"""ECS container features of a multi-container task: entryPoint, dependsOn, task volumes
without a host path, one network namespace per awsvpc task, initProcessEnabled and
healthCheck.

The first half drives the task worker against a recording Docker double and needs no daemon.
The second half starts real containers and is skipped without one.
"""

import json
import time
import uuid
from types import SimpleNamespace

import pytest
from test_ecs import _wait_until, requires_ecs_docker

from ministack.services import ecs as _ecs

SELF_ID = "ministack-self"
SELF_IP = "172.30.0.2"
HOLDER_IP = "172.30.0.50"


class _Container:
    def __init__(self, daemon, image, kwargs):
        self._daemon = daemon
        self.image = image
        self.kwargs = kwargs
        self.name = kwargs.get("name", "")
        self.id = uuid.uuid4().hex * 2
        self.status = "running"
        self.exit_code = None
        self.health = None
        self.removed = False

    @property
    def attrs(self):
        state = {"Status": self.status, "ExitCode": self.exit_code or 0}
        if self.health:
            state["Health"] = {"Status": self.health}
        ip = HOLDER_IP if self.name.endswith(".pause") else "172.30.0.%d" % (60 + len(self._daemon.runs))
        networks = {} if self.kwargs.get("network_mode") else {"ministack_default": {"IPAddress": ip}}
        return {"State": state, "NetworkSettings": {"Networks": networks}}

    def reload(self):
        pass

    def wait(self, **_):
        return {"StatusCode": self.exit_code or 0}

    def logs(self, **_):
        return b""

    def stop(self, **_):
        self.status = "exited"

    def remove(self, **_):
        self.removed = True
        self.status = "exited"

    def finish(self, code=0):
        self.exit_code = code
        self.status = "exited"


class _Daemon:
    """Records what the worker asks Docker for; containers stay up until a test finishes them."""

    def __init__(self):
        self.runs = []
        self.volumes_created = []
        self.volumes_removed = []
        me = SimpleNamespace(
            id=SELF_ID,
            attrs={"NetworkSettings": {"Networks": {"ministack_default": {"IPAddress": SELF_IP}}}},
        )
        self._by_id = {SELF_ID: me}
        self.containers = SimpleNamespace(get=self._get, list=lambda *a, **k: [], run=self._run)
        self.volumes = SimpleNamespace(create=self._volume_create, get=self._volume_get)
        self.images = SimpleNamespace(
            get=lambda image: SimpleNamespace(id="sha256:" + "a" * 64),
            pull=lambda image, **k: SimpleNamespace(tag=lambda *a, **kw: True),
            get_registry_data=lambda image: SimpleNamespace(id="sha256:" + "a" * 64),
        )

    def _get(self, ref):
        if ref not in self._by_id:
            raise Exception("not found")
        return self._by_id[ref]

    def _run(self, image, **kwargs):
        container = _Container(self, image, kwargs)
        self.runs.append(container)
        self._by_id[container.id] = container
        return container

    def _volume_create(self, name=None, **kwargs):
        self.volumes_created.append((name, kwargs))
        return SimpleNamespace(name=name, remove=lambda **k: self.volumes_removed.append(name))

    def _volume_get(self, name):
        return SimpleNamespace(name=name, remove=lambda **k: self.volumes_removed.append(name))

    def named(self, container_name):
        for container in self.runs:
            if container.name.endswith("-" + container_name):
                return container
        return None

    def started(self, container_name):
        return self.named(container_name) is not None


@pytest.fixture
def daemon(monkeypatch):
    fake = _Daemon()
    monkeypatch.setenv("HOSTNAME", SELF_ID)
    monkeypatch.setattr(_ecs, "_get_docker", lambda: fake)
    return fake


def _run(td_family, containers, **td):
    _ecs._register_task_definition({"family": td_family, "containerDefinitions": containers, **td})
    body = _ecs._run_task({"cluster": td_family + "-c", "taskDefinition": td_family})[2]
    arn = json.loads(body)["tasks"][0]["taskArn"]
    return _ecs._tasks[arn]


def _stopped(task):
    return task["lastStatus"] == "STOPPED"


def test_ecs_entrypoint_init_and_health_check_reach_docker_run(daemon):
    task = _run("ctr-run-kwargs", [{
        "name": "app", "image": "busybox",
        "entryPoint": ["/ctl/busybox", "sh", "-c"],
        "command": ["true"],
        "linuxParameters": {"initProcessEnabled": True},
        "healthCheck": {"command": ["CMD-SHELL", "true"], "interval": 5, "timeout": 2,
                        "retries": 4, "startPeriod": 7},
    }])
    _wait_until(lambda: daemon.started("app"))
    kwargs = daemon.named("app").kwargs
    assert kwargs["entrypoint"] == ["/ctl/busybox", "sh", "-c"]
    assert kwargs["command"] == ["true"]
    assert kwargs["init"] is True
    assert kwargs["healthcheck"] == {
        "test": ["CMD-SHELL", "true"], "interval": 5_000_000_000, "timeout": 2_000_000_000,
        "retries": 4, "start_period": 7_000_000_000,
    }
    # ECS's default timeout is 5 seconds; Docker's is 30.
    assert _ecs._docker_healthcheck({"command": ["CMD", "true"]}) == {
        "test": ["CMD", "true"], "timeout": 5_000_000_000}
    _ecs._stop_task({"cluster": "ctr-run-kwargs-c", "task": task["taskArn"]})


def test_ecs_without_those_options_docker_run_is_unchanged(daemon):
    task = _run("ctr-run-plain", [{"name": "app", "image": "busybox"}])
    _wait_until(lambda: daemon.started("app"))
    kwargs = daemon.named("app").kwargs
    assert not {"entrypoint", "init", "healthcheck"} & kwargs.keys()
    _ecs._stop_task({"cluster": "ctr-run-plain-c", "task": task["taskArn"]})


def test_ecs_named_task_volume_is_one_docker_volume_per_task(daemon):
    task = _run("ctr-volume", [
        {"name": "writer", "image": "busybox", "essential": False,
         "mountPoints": [{"sourceVolume": "ctl", "containerPath": "/ctl"}]},
        {"name": "reader", "image": "busybox",
         "mountPoints": [{"sourceVolume": "ctl", "containerPath": "/in", "readOnly": True}]},
    ], volumes=[{"name": "ctl"}])
    _wait_until(lambda: daemon.started("reader"))
    volume = f"ministack-ecs-{task['taskArn'].rsplit('/', 1)[-1][:8]}-ctl"
    assert [name for name, _ in daemon.volumes_created] == [volume]
    # nocopy: an empty ECS volume hides the image's own files at the path, as a bind mount does.
    assert daemon.named("writer").kwargs["volumes"] == [f"{volume}:/ctl:rw,nocopy"]
    assert daemon.named("reader").kwargs["volumes"] == [f"{volume}:/in:ro,nocopy"]
    _ecs._stop_task({"cluster": "ctr-volume-c", "task": task["taskArn"]})
    assert daemon.volumes_removed == [volume]


def test_ecs_host_path_volume_is_still_a_bind_mount(daemon):
    task = _run("ctr-hostpath", [
        {"name": "app", "image": "busybox",
         "mountPoints": [{"sourceVolume": "src", "containerPath": "/src"}]},
    ], volumes=[{"name": "src", "host": {"sourcePath": "/srv/data"}}])
    _wait_until(lambda: daemon.started("app"))
    assert daemon.named("app").kwargs["volumes"] == ["/srv/data:/src:rw"]
    assert daemon.volumes_created == []
    _ecs._stop_task({"cluster": "ctr-hostpath-c", "task": task["taskArn"]})


def test_ecs_awsvpc_containers_share_one_network_namespace(daemon):
    task = _run("ctr-netns", [
        {"name": "carrier", "image": "busybox", "essential": False},
        {"name": "main", "image": "busybox"},
    ], networkMode="awsvpc")
    _wait_until(lambda: daemon.started("main"))
    holder = daemon.runs[0]
    assert holder.name.endswith(".pause")
    assert holder.kwargs["network"] == "ministack_default"
    for name in ("carrier", "main"):
        kwargs = daemon.named(name).kwargs
        assert kwargs["network_mode"] == f"container:{holder.id}"
        assert "network" not in kwargs
        assert "extra_hosts" not in kwargs
    _wait_until(lambda: task.get("attachments"))
    details = {d["name"]: d["value"] for d in task["attachments"][0]["details"]}
    assert details["privateIPv4Address"] == HOLDER_IP
    # The holder is no container of the task.
    assert len(task["containers"]) == 2
    _ecs._stop_task({"cluster": "ctr-netns-c", "task": task["taskArn"]})
    assert holder.removed


def test_ecs_a_carrier_that_exits_does_not_take_the_task_address_along(daemon):
    task = _run("ctr-netns-exit", [
        {"name": "carrier", "image": "busybox", "essential": False},
        {"name": "main", "image": "busybox",
         "dependsOn": [{"containerName": "carrier", "condition": "SUCCESS"}]},
    ], networkMode="awsvpc")
    _wait_until(lambda: daemon.started("carrier"))
    daemon.named("carrier").finish(0)
    _wait_until(lambda: daemon.started("main"))
    _wait_until(lambda: task["lastStatus"] == "RUNNING")
    assert task["attachments"][0]["details"][-1]["value"] == HOLDER_IP
    _ecs._stop_task({"cluster": "ctr-netns-exit-c", "task": task["taskArn"]})


def test_ecs_single_container_awsvpc_task_needs_no_holder(daemon):
    task = _run("ctr-netns-one", [{"name": "app", "image": "busybox"}], networkMode="awsvpc")
    _wait_until(lambda: daemon.started("app"))
    assert len(daemon.runs) == 1
    assert daemon.named("app").kwargs["network"] == "ministack_default"
    _ecs._stop_task({"cluster": "ctr-netns-one-c", "task": task["taskArn"]})


def test_ecs_bridge_containers_keep_their_own_addresses(daemon):
    task = _run("ctr-bridge", [
        {"name": "a", "image": "busybox"}, {"name": "b", "image": "busybox"},
    ])
    _wait_until(lambda: daemon.started("b"))
    assert not any(c.name.endswith(".pause") for c in daemon.runs)
    _ecs._stop_task({"cluster": "ctr-bridge-c", "task": task["taskArn"]})


def test_ecs_depends_on_orders_startup_by_dependency_not_by_listing(daemon):
    task = _run("ctr-order", [
        {"name": "last", "image": "busybox",
         "dependsOn": [{"containerName": "middle", "condition": "START"}]},
        {"name": "middle", "image": "busybox",
         "dependsOn": [{"containerName": "first", "condition": "START"}]},
        {"name": "first", "image": "busybox", "essential": False},
    ])
    _wait_until(lambda: task["lastStatus"] == "RUNNING")
    assert [c.name.rsplit("-", 1)[-1] for c in daemon.runs] == ["first", "middle", "last"]
    # Each container's record is its own, whatever order they started in.
    assert all(c["lastStatus"] == "RUNNING" for c in task["containers"])
    daemon.named("first").finish(2)
    by_name = {c["name"]: c for c in task["containers"]}
    _wait_until(lambda: by_name["first"]["exitCode"] == 2)
    assert task["lastStatus"] == "RUNNING"
    assert by_name["middle"]["exitCode"] is None and by_name["last"]["exitCode"] is None
    _ecs._stop_task({"cluster": "ctr-order-c", "task": task["taskArn"]})


def test_ecs_depends_on_complete_waits_for_the_exit_of_any_code(daemon):
    task = _run("ctr-complete", [
        {"name": "prepare", "image": "busybox", "essential": False},
        {"name": "build", "image": "busybox", "essential": False,
         "dependsOn": [{"containerName": "prepare", "condition": "COMPLETE"}]},
    ])
    _wait_until(lambda: daemon.started("prepare"))
    time.sleep(0.5)
    assert not daemon.started("build")
    assert task["lastStatus"] != "RUNNING"
    daemon.named("prepare").finish(7)
    _wait_until(lambda: daemon.started("build"))
    _wait_until(lambda: task["lastStatus"] == "RUNNING")
    assert task["containers"][0]["exitCode"] == 7
    _ecs._stop_task({"cluster": "ctr-complete-c", "task": task["taskArn"]})


def test_ecs_depends_on_success_with_a_failed_dependency_fails_the_task(daemon):
    task = _run("ctr-success", [
        {"name": "carrier", "image": "busybox", "essential": False},
        {"name": "main", "image": "busybox",
         "dependsOn": [{"containerName": "carrier", "condition": "SUCCESS"}]},
    ])
    _wait_until(lambda: daemon.started("carrier"))
    daemon.named("carrier").finish(3)
    _wait_until(lambda: _stopped(task))
    assert not daemon.started("main")
    assert task["stopCode"] == "TaskFailedToStart"
    assert "carrier" in task["stoppedReason"] and "SUCCESS" in task["stoppedReason"]
    assert daemon.named("carrier").removed


def test_ecs_depends_on_healthy_waits_for_the_health_check(daemon):
    task = _run("ctr-healthy", [
        {"name": "db", "image": "busybox",
         "healthCheck": {"command": ["CMD-SHELL", "true"]}},
        {"name": "app", "image": "busybox",
         "dependsOn": [{"containerName": "db", "condition": "HEALTHY"}]},
    ])
    _wait_until(lambda: daemon.started("db"))
    daemon.named("db").health = "starting"
    time.sleep(0.5)
    assert not daemon.started("app")
    daemon.named("db").health = "healthy"
    _wait_until(lambda: daemon.started("app"))
    _ecs._stop_task({"cluster": "ctr-healthy-c", "task": task["taskArn"]})


def test_ecs_depends_on_healthy_with_an_unhealthy_dependency_fails_the_task(daemon):
    task = _run("ctr-unhealthy", [
        {"name": "db", "image": "busybox",
         "healthCheck": {"command": ["CMD-SHELL", "false"]}},
        {"name": "app", "image": "busybox",
         "dependsOn": [{"containerName": "db", "condition": "HEALTHY"}]},
    ])
    _wait_until(lambda: daemon.started("db"))
    daemon.named("db").health = "unhealthy"
    _wait_until(lambda: _stopped(task))
    assert not daemon.started("app")
    assert task["stopCode"] == "TaskFailedToStart"


def test_ecs_an_essential_exit_while_a_container_waits_stops_the_task(daemon):
    task = _run("ctr-essential-wait", [
        {"name": "setup", "image": "busybox", "essential": True},
        {"name": "next", "image": "busybox",
         "dependsOn": [{"containerName": "setup", "condition": "COMPLETE"}]},
    ])
    _wait_until(lambda: daemon.started("setup"))
    daemon.named("setup").finish(0)
    _wait_until(lambda: _stopped(task))
    assert not daemon.started("next")
    assert task["stopCode"] == "EssentialContainerExited"


def test_ecs_start_timeout_of_the_dependency_fails_the_task(daemon):
    task = _run("ctr-timeout", [
        {"name": "slow", "image": "busybox", "essential": False, "startTimeout": 1},
        {"name": "next", "image": "busybox",
         "dependsOn": [{"containerName": "slow", "condition": "COMPLETE"}]},
    ])
    _wait_until(lambda: _stopped(task), timeout=10)
    assert not daemon.started("next")
    assert task["stopCode"] == "TaskFailedToStart"


def test_ecs_stop_task_ends_a_dependency_wait(daemon):
    task = _run("ctr-stop-wait", [
        {"name": "slow", "image": "busybox", "essential": False},
        {"name": "next", "image": "busybox",
         "dependsOn": [{"containerName": "slow", "condition": "COMPLETE"}]},
    ])
    _wait_until(lambda: daemon.started("slow"))
    _ecs._stop_task({"cluster": "ctr-stop-wait-c", "task": task["taskArn"]})
    _wait_until(lambda: daemon.named("slow").removed)
    time.sleep(0.5)
    assert not daemon.started("next")


def test_ecs_depends_on_an_undefined_container_fails_the_task(daemon):
    task = _run("ctr-missing-dep", [
        {"name": "app", "image": "busybox",
         "dependsOn": [{"containerName": "ghost", "condition": "START"}]},
    ])
    _wait_until(lambda: _stopped(task))
    assert daemon.runs == []
    assert task["stopCode"] == "TaskFailedToStart"


# ---------------------------------------------------------------------------
# Real containers
# ---------------------------------------------------------------------------

def _real_task(ecs, name, containers, **td):
    cluster = f"{name}-{uuid.uuid4().hex[:8]}"
    ecs.create_cluster(clusterName=cluster)
    ecs.register_task_definition(family=cluster, containerDefinitions=containers, **td)
    arn = ecs.run_task(cluster=cluster, taskDefinition=cluster)["tasks"][0]["taskArn"]
    return cluster, arn


def _await_stopped(ecs, cluster, arn, timeout=90):
    task = {}

    def stopped():
        nonlocal task
        task = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]
        return task["lastStatus"] == "STOPPED"

    _wait_until(stopped, timeout=timeout)
    return task, {c["name"]: {k: c.get(k) for k in ("lastStatus", "exitCode")} | {
        "task": (task.get("stopCode"), task.get("stoppedReason"))} for c in task["containers"]}


@requires_ecs_docker
@pytest.mark.data_plane
def test_ecs_carrier_pattern_runs_with_real_containers(ecs):
    """The envd carrier shape: one container fills a task volume and exits, the main container
    waits for it, starts through an entryPoint and reaches a sibling over the task's address."""
    cluster, arn = _real_task(ecs, "ctr-carrier", [
        {"name": "carrier", "image": "alpine:latest", "essential": False,
         "entryPoint": ["sh", "-c"], "command": ["cp /bin/busybox /ctl/busybox && echo ready > /ctl/state"],
         "mountPoints": [{"sourceVolume": "ctl", "containerPath": "/ctl"}]},
        {"name": "server", "image": "alpine:latest", "essential": False,
         "entryPoint": ["/ctl/busybox", "sh", "-c"],
         "command": ["while :; do /ctl/busybox cat /ctl/state | /ctl/busybox nc -l -p 8080; done"],
         "dependsOn": [{"containerName": "carrier", "condition": "SUCCESS"}],
         "mountPoints": [{"sourceVolume": "ctl", "containerPath": "/ctl", "readOnly": True}]},
        {"name": "main", "image": "alpine:latest",
         "entryPoint": ["sh", "-c"],
         "command": ["sleep 2; test \"$(nc 127.0.0.1 8080 </dev/null)\" = ready"],
         "dependsOn": [{"containerName": "server", "condition": "START"}],
         "linuxParameters": {"initProcessEnabled": True}},
    ], networkMode="awsvpc", volumes=[{"name": "ctl"}])
    task, containers = _await_stopped(ecs, cluster, arn)
    assert containers["carrier"]["exitCode"] == 0
    assert containers["main"]["exitCode"] == 0, containers
    assert task["stopCode"] == "EssentialContainerExited"
    ecs.delete_cluster(cluster=cluster)


@requires_ecs_docker
@pytest.mark.data_plane
def test_ecs_awsvpc_task_has_one_address_that_outlives_its_first_container(ecs):
    import docker

    cluster, arn = _real_task(ecs, "ctr-addr", [
        {"name": "carrier", "image": "alpine:latest", "essential": False,
         "command": ["sh", "-c", "true"]},
        {"name": "main", "image": "alpine:latest",
         "dependsOn": [{"containerName": "carrier", "condition": "COMPLETE"}],
         "command": ["sh", "-c", "sleep 30"]},
    ], networkMode="awsvpc")
    _wait_until(lambda: ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]["lastStatus"] == "RUNNING",
                timeout=60)
    task = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]
    details = {d["name"]: d["value"] for d in task["attachments"][0]["details"]}
    client = docker.from_env()
    prefix = f"ministack-ecs-{arn.rsplit('/', 1)[-1][:8]}"
    main = client.containers.get(f"{prefix}-main")
    holder = client.containers.get(f"{prefix}.pause")
    assert main.attrs["HostConfig"]["NetworkMode"] == f"container:{holder.id}"
    nets = holder.attrs["NetworkSettings"]["Networks"]
    assert details["privateIPv4Address"] in {n["IPAddress"] for n in nets.values()}
    out = main.exec_run(["sh", "-c", "hostname -i"]).output.decode().split()
    assert details["privateIPv4Address"] in out
    ecs.stop_task(cluster=cluster, task=arn)
    ecs.delete_cluster(cluster=cluster)


@requires_ecs_docker
@pytest.mark.data_plane
def test_ecs_complete_chain_runs_in_order_and_task_waits_for_the_last(ecs):
    """The unit-scanner shape: four containers, each started after the one before it exited."""
    def step(name, after, command, **extra):
        return {
            "name": name, "image": "alpine:latest", "command": ["sh", "-c", command],
            "mountPoints": [{"sourceVolume": "work", "containerPath": "/work"}],
            **({"dependsOn": [{"containerName": after, "condition": "COMPLETE"}]} if after else {}),
            **extra,
        }

    cluster, arn = _real_task(ecs, "ctr-chain", [
        step("prepare", None, "sleep 1; echo a >> /work/log", essential=False),
        step("build", "prepare", "sleep 1; echo b >> /work/log; exit 9", essential=False),
        step("scan", "build", "sleep 1; echo c >> /work/log", essential=False),
        step("collect", "scan", 'test "$(cat /work/log | tr -d "\\n")" = abc'),
    ], volumes=[{"name": "work"}])
    time.sleep(1.5)
    early = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]
    assert early["lastStatus"] != "RUNNING"
    task, containers = _await_stopped(ecs, cluster, arn)
    assert containers["build"]["exitCode"] == 9
    assert containers["collect"]["exitCode"] == 0, containers
    ecs.delete_cluster(cluster=cluster)


@requires_ecs_docker
@pytest.mark.data_plane
def test_ecs_depends_on_success_does_not_start_after_a_failed_dependency(ecs):
    cluster, arn = _real_task(ecs, "ctr-fail", [
        {"name": "carrier", "image": "alpine:latest", "essential": False,
         "command": ["sh", "-c", "exit 3"]},
        {"name": "main", "image": "alpine:latest", "command": ["sh", "-c", "sleep 30"],
         "dependsOn": [{"containerName": "carrier", "condition": "SUCCESS"}]},
    ])
    task, containers = _await_stopped(ecs, cluster, arn)
    assert containers["carrier"]["exitCode"] == 3
    assert containers["main"].get("exitCode") is None
    assert task["stopCode"] == "TaskFailedToStart"
    ecs.delete_cluster(cluster=cluster)


@requires_ecs_docker
@pytest.mark.data_plane
def test_ecs_depends_on_healthy_waits_for_a_real_health_check(ecs):
    cluster, arn = _real_task(ecs, "ctr-health", [
        {"name": "db", "image": "alpine:latest", "essential": False,
         "command": ["sh", "-c", "sleep 3; touch /work/ready; touch /tmp/ok; sleep 60"],
         "mountPoints": [{"sourceVolume": "work", "containerPath": "/work"}],
         "healthCheck": {"command": ["CMD-SHELL", "test -f /tmp/ok"], "interval": 1, "timeout": 2,
                         "retries": 3, "startPeriod": 15}},
        {"name": "app", "image": "alpine:latest", "command": ["sh", "-c", "test -f /work/ready"],
         "mountPoints": [{"sourceVolume": "work", "containerPath": "/work"}],
         "dependsOn": [{"containerName": "db", "condition": "HEALTHY"}]},
    ], volumes=[{"name": "work"}])
    task, containers = _await_stopped(ecs, cluster, arn)
    assert containers["app"]["exitCode"] == 0, containers
    ecs.delete_cluster(cluster=cluster)


def test_every_container_gets_the_region_and_launch_type_ecs_sets(monkeypatch):
    from ministack.services import ecs_metadata

    monkeypatch.setattr(ecs_metadata, "register_container", lambda *a, **k: None)
    task_arn = "arn:aws:ecs:eu-west-1:000000000000:task/c/1"
    td = {"family": "f", "revision": 1}
    env = {}
    _ecs._register_metadata(task_arn, "cluster", td, {"name": "app", "image": "i"}, "FARGATE",
                            env, False, "169.254.170.23")
    region = env["AWS_REGION"]
    assert region and env["AWS_DEFAULT_REGION"] == region
    assert env["AWS_EXECUTION_ENV"] == "AWS_ECS_FARGATE"
    # The container definition's own value wins.
    env = {"AWS_REGION": "us-west-2"}
    _ecs._register_metadata(task_arn, "cluster", td, {"name": "app", "image": "i"}, "EC2",
                            env, False, "169.254.170.23")
    assert env["AWS_REGION"] == "us-west-2"
    assert env["AWS_EXECUTION_ENV"] == "AWS_ECS_EC2"


def test_a_task_id_has_the_form_ecs_gives_it(ecs):
    import re

    ecs.create_cluster(clusterName="ids")
    ecs.register_task_definition(
        family="ids", containerDefinitions=[{"name": "app", "image": "busybox", "memory": 64}]
    )
    task = ecs.run_task(cluster="ids", taskDefinition="ids")["tasks"][0]
    assert re.fullmatch("[0-9a-f]{32}", task["taskArn"].rsplit("/", 1)[-1])


def test_containers_can_get_credentials_as_fargate_hands_them_over(monkeypatch):
    from ministack.services import ecs_metadata

    monkeypatch.setattr(ecs_metadata, "register_container", lambda *a, **k: None)
    task_arn = "arn:aws:ecs:eu-west-1:000000000000:task/c/1"
    td = {"family": "f", "revision": 1}
    env = {}
    _ecs._register_metadata(task_arn, "cluster", td, {"name": "app", "image": "i"}, "FARGATE",
                            env, False, "169.254.170.23")
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"].startswith("http://169.254.170.23:")
    assert env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in env

    monkeypatch.setenv("ECS_CONTAINER_CREDENTIALS", "relative")
    env = {}
    _ecs._register_metadata(task_arn, "cluster", td, {"name": "app", "image": "i"}, "FARGATE",
                            env, False, "169.254.170.23")
    assert env["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"].startswith("/v2/credentials/")
    assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in env
    assert "AWS_CONTAINER_AUTHORIZATION_TOKEN" not in env
