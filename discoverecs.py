from __future__ import print_function
from collections import defaultdict
import boto3
import json
import argparse
import time
import os
import re

"""
Copyright 2018, 2019, 2020 Signal Media Ltd

ECS service discovery for tasks. Please enable it by setting env variable
PROMETHEUS to "true".

Metric path and scape interval is supported via PROMETHEUS_ENDPOINT:

"interval:/metric_path,..."

Examples:

"5m:/mymetrics,30s:/mymetrics2"
"/mymetrics"
"30s:/mymetrics1,/mymetrics2"

Under ECS task definition (task.json):

{"name": "PROMETHEUS_ENDPOINT", "value": "5m:/mymetrics,30s:/mymetrics2"}

Available intervals: 15s, 30s, 1m, 5m.

Default metric path is /metrics. Default interval is 1m.

For skipping labels, set PROMETHEUS_NOLABELS to "true".
This is useful when you use "blackbox" exporters or Pushgateway in a task
and metrics are exposed at a service level. This way, no ec2/ecs labels
will be exposed and the instance label will always point to the job name.

PROMETHEUS_PORT must be set when using awsvpc network mode. It can also be used
for tasks using a classic ELB setup with multiple port mappings.
"""


def log(message):
    print(message)


def chunk_list(l, n):
    return [l[i : i + n] for i in range(0, len(l), n)]


def dict_get(d, k, default):
    if k in d:
        return d[k]
    else:
        return default


class FlipCache:
    def __init__(self):
        self.current_cache = {}
        self.next_cache = {}
        self.hits = 0
        self.misses = 0

    def flip(self):
        self.current_cache = self.next_cache
        self.next_cache = {}
        self.hits = 0
        self.misses = 0

    def get_dict(self, keys, fetcher):
        missing = []
        result = {}
        for k in set(keys):
            if k in self.current_cache:
                result[k] = self.current_cache[k]
                self.hits += 1
            else:
                missing += [k]
                self.misses += 1
        fetched = fetcher(missing) if missing else {}
        result.update(fetched)
        self.current_cache.update(fetched)
        self.next_cache.update(result)
        return result

    def get(self, key, fetcher):
        if key in self.current_cache:
            result = self.current_cache[key]
            self.hits += 1
        else:
            self.misses += 1
            result = fetcher(key)
        if result:
            self.current_cache[key] = result
            self.next_cache[key] = result
        return result


class TaskInfo:
    def __init__(self, task):
        self.task = task
        self.task_definition = None
        self.container_instance = None
        self.ec2_instance = None

    def valid(self):
        if "FARGATE" in self.task_definition.get("requiresCompatibilities", ""):
            return self.task_definition
        else:
            return (
                self.task_definition and self.container_instance and self.ec2_instance
            )


class TaskInfoDiscoverer:
    def __init__(self, fetch_tags=True, cluster_arns=[]):
        self.ec2_client = boto3.client("ec2")
        self.ecs_client = boto3.client("ecs")
        self.task_cache = FlipCache()
        self.task_definition_cache = FlipCache()
        self.container_instance_cache = FlipCache()
        self.ec2_instance_cache = FlipCache()
        self.fetch_tags = fetch_tags
        self.cluster_arns = cluster_arns

    def flip_caches(self):
        self.task_cache.flip()
        self.task_definition_cache.flip()
        self.container_instance_cache.flip()
        self.ec2_instance_cache.flip()

    def describe_tasks(self, cluster_arn, task_arns, service_arn):
        def fetcher_task_definition(arn):
            response = self.ecs_client.describe_task_definition(
                taskDefinition=arn,
                include=[
                    "TAGS",
                ]
                if self.fetch_tags
                else [],
            )

            return {**response["taskDefinition"], "tags": response.get("tags", [])}

        def fetcher(fetch_task_arns):
            tasks = {}
            result = self.ecs_client.describe_tasks(
                cluster=cluster_arn,
                tasks=fetch_task_arns,
                include=[
                    "TAGS",
                ]
                if self.fetch_tags
                else [],
            )
            if "tasks" in result:
                for task in result["tasks"]:
                    no_network_binding = []
                    for container in task["containers"]:
                        if (
                            "networkBindings" not in container
                            or len(container["networkBindings"]) == 0
                        ) and len(container["networkInterfaces"]) == 0:
                            no_network_binding.append(container["name"])
                    if no_network_binding:
                        arn = task["taskDefinitionArn"]
                        no_cache = None
                        task_definition = self.task_definition_cache.get(
                            arn, fetcher_task_definition
                        )
                        is_host_network_mode = (
                            task_definition.get("networkMode") == "host"
                        )
                        for container_definition in task_definition[
                            "containerDefinitions"
                        ]:
                            prometheus = get_environment_var(
                                container_definition["environment"], "PROMETHEUS"
                            )
                            prometheus_port = get_environment_var(
                                container_definition["environment"], "PROMETHEUS_PORT"
                            )
                            port_mappings = container_definition.get("portMappings")
                            if (
                                container_definition["name"] in no_network_binding
                                and prometheus
                                and not (
                                    is_host_network_mode
                                    and (prometheus_port or port_mappings)
                                )
                            ):
                                log(
                                    task["group"]
                                    + ":"
                                    + container_definition["name"]
                                    + " does not have a networkBinding. Skipping for next run."
                                )
                                no_cache = True
                        if not no_cache:
                            tasks[task["taskArn"]] = task
                    else:
                        tasks[task["taskArn"]] = task
            tasks[task["taskArn"]]["service"]=service_arn
            return tasks

        return self.task_cache.get_dict(task_arns, fetcher).values()

    def create_task_infos(self, cluster_arn, task_arns, service_arn):
        return map(lambda t: TaskInfo(t), self.describe_tasks(cluster_arn, task_arns, service_arn))

    def add_task_definitions(self, task_infos):
        def fetcher(arn):
            return self.ecs_client.describe_task_definition(taskDefinition=arn)[
                "taskDefinition"
            ]

        for task_info in task_infos:
            arn = task_info.task["taskDefinitionArn"]
            task_info.task_definition = self.task_definition_cache.get(arn, fetcher)

    def add_container_instances(self, task_infos, cluster_arn):
        def fetcher(arns):
            arnsChunked = chunk_list(arns, 100)
            instances = {}
            for arns in arnsChunked:
                result = self.ecs_client.describe_container_instances(
                    cluster=cluster_arn, containerInstances=arns
                )
                for i in dict_get(result, "containerInstances", []):
                    instances[i["containerInstanceArn"]] = i
            return instances

        containerInstanceArns = list(
            set(map(lambda t: t.task["containerInstanceArn"], task_infos))
        )
        containerInstances = self.container_instance_cache.get_dict(
            containerInstanceArns, fetcher
        )
        for t in task_infos:
            t.container_instance = dict_get(
                containerInstances, t.task["containerInstanceArn"], None
            )

    def add_ec2_instances(self, task_infos):
        def fetcher(ids):
            idsChunked = chunk_list(ids, 100)
            instances = {}
            for ids in idsChunked:
                result = self.ec2_client.describe_instances(InstanceIds=ids)
                for r in dict_get(result, "Reservations", []):
                    for i in dict_get(r, "Instances", []):
                        instances[i["InstanceId"]] = i
            return instances

        instance_ids = list(
            set(map(lambda t: t.container_instance["ec2InstanceId"], task_infos))
        )
        instances = self.ec2_instance_cache.get_dict(instance_ids, fetcher)
        for t in task_infos:
            t.ec2_instance = dict_get(
                instances, t.container_instance["ec2InstanceId"], None
            )

    def get_infos_for_cluster(self, cluster_arn, launch_type):
        service_pages = self.ecs_client.get_paginator('list_services').paginate(
            cluster=cluster_arn
        )
        task_infos = []
        for service_arns in service_pages:
            for service_arn in service_arns["serviceArns"]:
                tasks_pages = self.ecs_client.get_paginator("list_tasks").paginate(
                    cluster=cluster_arn, launchType=launch_type, serviceName=service_arn
                )
                for task_arns in tasks_pages:
                    if task_arns["taskArns"]:
                        task_infos += self.create_task_infos(cluster_arn, task_arns["taskArns"], service_arn)
                self.add_task_definitions(task_infos)
                if "EC2" in launch_type:
                    self.add_container_instances(task_infos, cluster_arn)
        return task_infos

    def print_cache_stats(self):
        log(
            "task_cache {} {} task_definition_cache {} {} {} container_instance_cache {} {} ec2_instance_cache {} {} {}".format(
                self.task_cache.hits,
                self.task_cache.misses,
                self.task_definition_cache.hits,
                self.task_definition_cache.misses,
                len(self.task_definition_cache.current_cache),
                self.container_instance_cache.hits,
                self.container_instance_cache.misses,
                self.ec2_instance_cache.hits,
                self.ec2_instance_cache.misses,
                len(self.ec2_instance_cache.current_cache),
            )
        )

    def list_clusters(self):
        if len(self.cluster_arns) > 0:
            return self.cluster_arns
        cluster_arns = []
        clusters_pages = self.ecs_client.get_paginator("list_clusters").paginate()
        for clusters in clusters_pages:
            for cluster_arn in clusters["clusterArns"]:
                cluster_arns += [cluster_arn]

        return cluster_arns

    def get_infos(self):
        self.flip_caches()
        task_infos = []
        fargate_task_infos = []
        cluster_arns = self.list_clusters()
        for cluster_arn in cluster_arns:
            task_infos += self.get_infos_for_cluster(cluster_arn, "EC2")
            fargate_task_infos += self.get_infos_for_cluster(cluster_arn, "FARGATE")
        self.add_ec2_instances(task_infos)
        task_infos += fargate_task_infos
        self.print_cache_stats()
        return task_infos


class Target:
    def __init__(
        self,
        ip,
        port,
        metrics_path,
        p_instance,
        ecs_task_id,
        ecs_task_name,
        ecs_service_name,
        ecs_task_version,
        ecs_container_id,
        ecs_cluster_name,
        ec2_instance_id,
        tags,
    ):
        self.ip = ip
        self.port = port
        self.metrics_path = metrics_path
        self.p_instance = p_instance
        self.ecs_task_id = ecs_task_id
        self.ecs_task_name = ecs_task_name
        self.ecs_service_name = ecs_service_name
        self.ecs_task_version = ecs_task_version
        self.ecs_container_id = ecs_container_id
        self.ecs_cluster_name = ecs_cluster_name
        self.ec2_instance_id = ec2_instance_id
        self.tags = tags


def get_environment_var(environment, name):
    for entry in environment:
        if entry["name"] == name:
            return entry["value"]
    return None


def extract_name_from_arn(arn):
    return arn.split(":")[5].split("/")[-1]


def extract_task_version(taskDefinitionArn):
    return taskDefinitionArn.split(":")[6]


def extract_path_interval(env_variable):
    path_interval = {}
    if env_variable:
        for lst in env_variable.split(","):
            if ":" in lst:
                pi = lst.split(":")
                if re.search("(15s|30s|1m|5m)", pi[0]):
                    path_interval[pi[1]] = pi[0]
                else:
                    path_interval[pi[1]] = None
            else:
                path_interval[lst] = None
    else:
        path_interval["/metrics"] = None
    return path_interval


def task_info_to_targets(task_info):
    targets = []

    task = task_info.task
    task_definition = task_info.task_definition

    if not task_info.valid():
        return targets

    for container_definition in task_definition["containerDefinitions"]:
        prometheus_enabled = get_environment_var(
            container_definition["environment"], "PROMETHEUS"
        )
        metrics_path = get_environment_var(
            container_definition["environment"], "PROMETHEUS_ENDPOINT"
        )
        nolabels = get_environment_var(
            container_definition["environment"], "PROMETHEUS_NOLABELS"
        )
        if nolabels != "true":
            nolabels = None
        prometheus_port = get_environment_var(
            container_definition["environment"], "PROMETHEUS_PORT"
        )
        prometheus_container_port = get_environment_var(
            container_definition["environment"], "PROMETHEUS_CONTAINER_PORT"
        )
        running_containers = filter(
            lambda container: container["name"] == container_definition["name"],
            task["containers"],
        )

        if not prometheus_enabled:
            continue

        # get tags from the task definition, and merge/override any tags specifically set on the task
        tags = {
            **{tag["key"]: tag["value"] for tag in task_definition.get("tags", [])},
            **{tag["key"]: tag["value"] for tag in task.get("tags", [])},
        }

        for container in running_containers:
            ecs_task_name = extract_name_from_arn(task["taskDefinitionArn"])
            ecs_service_name = extract_name_from_arn(task["service"])
            has_host_port_mapping = (
                "portMappings" in container_definition
                and len(container_definition["portMappings"]) > 0
            )

            if prometheus_port:
                first_port = prometheus_port
            elif task_definition.get("networkMode") in ("host", "awsvpc"):
                if has_host_port_mapping:
                    first_port = str(
                        container_definition["portMappings"][0]["hostPort"]
                    )
                else:
                    first_port = "80"
            elif prometheus_container_port:
                binding_by_container_port = [
                    c
                    for c in container["networkBindings"]
                    if str(c["containerPort"]) == prometheus_container_port
                ]
                if binding_by_container_port:
                    first_port = str(binding_by_container_port[0]["hostPort"])
                else:
                    log(
                        task["group"]
                        + ":"
                        + container_definition["name"]
                        + " does not expose port matching PROMETHEUS_CONTAINER_PORT, omitting"
                    )
                    return []
            else:
                first_port = str(container["networkBindings"][0]["hostPort"])

            if task_definition.get("networkMode") == "awsvpc":
                interface_ip = container["networkInterfaces"][0]["privateIpv4Address"]
            else:
                interface_ip = task_info.ec2_instance["PrivateIpAddress"]

            if nolabels:
                p_instance = ecs_task_name
                ecs_task_id = (
                    ecs_task_version
                ) = ecs_container_id = ecs_cluster_name = ec2_instance_id = None
            else:
                p_instance = interface_ip + ":" + first_port
                ecs_task_id = extract_name_from_arn(task["taskArn"])
                ecs_task_version = extract_task_version(task["taskDefinitionArn"])
                ecs_cluster_name = extract_name_from_arn(task["clusterArn"])
                if "FARGATE" in task_definition.get("requiresCompatibilities", ""):
                    ec2_instance_id = ecs_container_id = None
                else:
                    ec2_instance_id = task_info.container_instance["ec2InstanceId"]
                    ecs_container_id = extract_name_from_arn(container["containerArn"])

            targets += [
                Target(
                    ip=interface_ip,
                    port=first_port,
                    metrics_path=metrics_path,
                    p_instance=p_instance,
                    ecs_task_id=ecs_task_id,
                    ecs_task_name=ecs_task_name,
                    ecs_service_name=ecs_service_name,
                    ecs_task_version=ecs_task_version,
                    ecs_container_id=ecs_container_id,
                    ecs_cluster_name=ecs_cluster_name,
                    ec2_instance_id=ec2_instance_id,
                    tags=tags,
                )
            ]
    return targets


class Main:
    def __init__(
        self, directory, interval, default_scrape_interval_prefix, tags_to_labels, cluster_arns
    ):
        self.directory = directory
        self.interval = interval
        self.default_scrape_interval_prefix = default_scrape_interval_prefix
        self.discoverer = TaskInfoDiscoverer(fetch_tags=len(tags_to_labels) > 0, cluster_arns=cluster_arns)
        self.tags_to_labels = tags_to_labels

    def write_jobs(self, jobs):
        for prefix, j in jobs.items():
            file_name = self.directory + "/" + prefix + "-tasks.json"
            tmp_file_name = file_name + ".tmp"
            with open(tmp_file_name, "w") as f:
                f.write(json.dumps(j, indent=4))
            os.rename(tmp_file_name, file_name)

    def get_targets(self):
        targets = []
        infos = self.discoverer.get_infos()
        for info in infos:
            targets += task_info_to_targets(info)
        return targets

    def discover_tasks(self):
        targets = self.get_targets()
        jobs = defaultdict(list)
        for i in ["15s", "30s", "1m", "5m"]:
            jobs[i] = []
        log("Targets: " + str(len(targets)))
        for target in targets:
            path_interval = extract_path_interval(target.metrics_path)
            for path, interval in path_interval.items():
                labels = False
                if target.ec2_instance_id is None and target.ecs_task_id:
                    labels = {
                        "ecs_task_id": target.ecs_task_id,
                        "ecs_task_version": target.ecs_task_version,
                        "ecs_cluster": target.ecs_cluster_name,
                    }
                elif target.ec2_instance_id:
                    labels = {
                        "ecs_task_id": target.ecs_task_id,
                        "ecs_task_version": target.ecs_task_version,
                        "ecs_container_id": target.ecs_container_id,
                        "ecs_cluster": target.ecs_cluster_name,
                        "instance_id": target.ec2_instance_id,
                    }
                job = {
                    "targets": [target.ip + ":" + target.port],
                    "labels": {
                        "instance": target.p_instance,
                        "job": target.ecs_service_name,
                        "task_definition":target.ecs_task_name,
                        "metrics_path": path,
                    },
                }
                for tag_name, tag_value in target.tags.items():
                    if not tag_name.lower().startswith("aws:") and (
                        tag_name in self.tags_to_labels or self.tags_to_labels == ["*"]
                    ):
                        # prometheus labels match [a-zA-Z_][a-zA-Z0-9_]*
                        # with leading __ reserved for internal use
                        tag_name = re.sub(r"[^a-zA-Z0-9_]", "_", tag_name).lstrip("_")
                        if tag_name != "" and not re.match(r"^[0-9]", tag_name):
                            job["labels"]["__meta_ecs_tag_" + tag_name] = tag_value
                if labels:
                    job["labels"].update(labels)
                jobs[interval or self.default_scrape_interval_prefix].append(job)
                log("Discovered Job: " + str(job))
        self.write_jobs(jobs)

    def loop(self):
        while True:
            self.discover_tasks()
            time.sleep(self.interval)

def main():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
        "--directory",
        required=True,
        help="The output directory for service discovery configs.",
    )
    arg_parser.add_argument(
        "--interval",
        type=float,
        default=60,
        help="The interval to refresh targetes from AWS APIs.",
    )
    arg_parser.add_argument(
        "--default-scrape-interval-prefix",
        default="1m",
        help="The default prefix to write the service discovery file for if no explicit prefix is specified in the discovered service.",
    )
    arg_parser.add_argument(
        "--tags-to-labels",
        nargs="*",
        default=[],
        help="Task definition tags to convert to labels. Case sensitive.",
    )
    arg_parser.add_argument(
        "--cluster-arns",
        nargs="*",
        default=[],
        help="The ARNs of the ECS clusters that should be monitored."
    )
    args = arg_parser.parse_args()
    log(
        f"""
Starting...
Directory: "{args.directory}"
Refresh interval: "{str(args.interval)}s"
Default scrape interval prefix: "{args.default_scrape_interval_prefix}"
Tags to convert to labels: {args.tags_to_labels}
Clusters to query: {args.cluster_arns}
        """
    )
    Main(
        directory=args.directory,
        interval=args.interval,
        default_scrape_interval_prefix=args.default_scrape_interval_prefix,
        tags_to_labels=args.tags_to_labels,
        cluster_arns=args.cluster_arns
    ).loop()


if __name__ == "__main__":
    main()
