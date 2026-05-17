import atexit
import re
import shlex
import time
import uuid

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (
    ContainerGroup,
    Container,
    ResourceRequests,
    ResourceRequirements,
    OperatingSystemTypes,
    ContainerGroupRestartPolicy,
    IpAddress,
    Port,
    ContainerPort,
    ImageRegistryCredential,
    ContainerGroupIdentity,
    ResourceIdentityType,
    UserAssignedIdentities,
)
from azure.containerregistry import ContainerRegistryClient, ArtifactTagOrder

ACI_MIN_CPU = 0.1
ACI_MAX_CPU = 4.0
ACI_MIN_MEM_GB = 0.1
ACI_MAX_MEM_GB = 16.0
MAX_TAGS_PER_REPO = 20

from CTFd.models import db
from .models import ContainerInfoModel
from .container_manager import ContainerException


class _CreatedContainer:
    """Stand-in for the docker SDK Container object so calling code stays uniform."""

    def __init__(self, id: str, hostname: str, port: int):
        self.id = id
        self.hostname = hostname
        self.port = port


REQUIRED_SETTINGS = (
    "azure_subscription_id",
    "azure_resource_group",
    "azure_region",
    "azure_uami_resource_id",
)


class ACIContainerManager:
    def __init__(self, settings, app):
        self.settings = settings
        self.app = app
        self.client = None
        self.acr_client = None
        self.credential = None
        self.expiration_seconds = 0
        self.expiration_scheduler = None

        if not self._has_required_settings():
            return

        try:
            self.initialize_connection(settings, app)
        except ContainerException:
            print("ACI could not initialize or connect.")

    def _has_required_settings(self) -> bool:
        return all(self.settings.get(k) for k in REQUIRED_SETTINGS)

    def initialize_connection(self, settings, app) -> None:
        self.settings = settings
        self.app = app

        try:
            if self.expiration_scheduler is not None:
                self.expiration_scheduler.shutdown()
        except (SchedulerNotRunningError, AttributeError):
            pass

        if not self._has_required_settings():
            self.client = None
            return

        try:
            self.credential = DefaultAzureCredential()
            self.client = ContainerInstanceManagementClient(
                self.credential, settings["azure_subscription_id"]
            )
        except Exception as e:
            self.client = None
            raise ContainerException(f"CTFd could not connect to Azure: {e}")

        try:
            self.expiration_seconds = int(settings.get("container_expiration", 0)) * 60
        except (ValueError, AttributeError):
            self.expiration_seconds = 0

        EXPIRATION_CHECK_INTERVAL = 5
        if self.expiration_seconds > 0:
            self.expiration_scheduler = BackgroundScheduler()
            self.expiration_scheduler.add_job(
                func=self.kill_expired_containers,
                args=(app,),
                trigger="interval",
                seconds=EXPIRATION_CHECK_INTERVAL,
            )
            self.expiration_scheduler.start()
            atexit.register(lambda: self.expiration_scheduler.shutdown())

    def is_connected(self) -> bool:
        if self.client is None:
            return False
        try:
            next(
                iter(
                    self.client.container_groups.list_by_resource_group(
                        self.settings["azure_resource_group"]
                    )
                ),
                None,
            )
            return True
        except Exception as e:
            print(f"[CTFd-ACI] is_connected failed: {e}")
            return False

    def shutdown(self) -> None:
        try:
            if self.expiration_scheduler is not None:
                self.expiration_scheduler.shutdown(wait=False)
        except (SchedulerNotRunningError, AttributeError):
            pass

    def is_container_running(self, container_id: str) -> bool:
        if self.client is None:
            return False
        try:
            cg = self.client.container_groups.get(
                self.settings["azure_resource_group"], container_id
            )
        except ResourceNotFoundError:
            return False
        except Exception as e:
            print(f"[CTFd-ACI] is_container_running({container_id}) failed: {e}")
            return False

        if cg.provisioning_state != "Succeeded":
            return False
        if not cg.containers:
            return False
        inst = cg.containers[0].instance_view
        if inst is None or inst.current_state is None:
            return False
        return inst.current_state.state == "Running"

    def kill_expired_containers(self, app: Flask):
        with app.app_context():
            now = int(time.time())
            containers = ContainerInfoModel.query.all()
            deleted = False
            for container in containers:
                if container.expires - now < 0:
                    if container.container_id:
                        try:
                            self.kill_container(container.container_id)
                        except ContainerException as e:
                            print(f"[CTFd-ACI] kill_expired_containers: {e}")
                    db.session.delete(container)
                    deleted = True
            if deleted:
                db.session.commit()

    def create_container(self, image: str, port: int, command: str, volumes: str, owner: str = None):
        if self.client is None:
            raise ContainerException("ACI client is not initialized")

        rg = self.settings["azure_resource_group"]
        region = self.settings["azure_region"]
        uami = self.settings["azure_uami_resource_id"]
        login_server = self.settings.get("acr_login_server", "")
        dns_prefix = self.settings.get("azure_dns_label_prefix", "ctfd")

        cpu = 1.0
        memory_gb = 1.5
        try:
            mem_mb = int(self.settings.get("container_maxmemory") or 0)
            if mem_mb > 0:
                memory_gb = max(ACI_MIN_MEM_GB, min(ACI_MAX_MEM_GB, mem_mb / 1024))
        except ValueError:
            pass
        # ACI requires memory to be in 0.1 GB increments and CPU in 0.01 increments.
        memory_gb = round(memory_gb * 10) / 10
        try:
            cpu_setting = float(self.settings.get("container_maxcpu") or 0)
            if cpu_setting > 0:
                cpu = max(ACI_MIN_CPU, min(ACI_MAX_CPU, cpu_setting))
        except ValueError:
            pass
        cpu = round(cpu * 100) / 100

        unique = uuid.uuid4().hex[:4]
        if owner:
            slug = re.sub(r"[^a-z0-9-]+", "-", owner.lower()).strip("-")
            slug = re.sub(r"-{2,}", "-", slug) or "user"
            group_name = f"{dns_prefix}-{slug}-{unique}"
        else:
            group_name = f"{dns_prefix}-{uuid.uuid4().hex[:8]}"
        group_name = group_name[:63].rstrip("-")
        dns_label = group_name

        command_list = None
        if command:
            try:
                command_list = shlex.split(command)
            except ValueError:
                command_list = [command]

        identity = ContainerGroupIdentity(
            type=ResourceIdentityType.USER_ASSIGNED,
            user_assigned_identities={uami: UserAssignedIdentities()},
        )
        image_registry_creds = []
        if login_server:
            image_registry_creds.append(
                ImageRegistryCredential(server=login_server, identity=uami)
            )

        container = Container(
            name="challenge",
            image=image,
            command=command_list,
            resources=ResourceRequirements(
                requests=ResourceRequests(memory_in_gb=memory_gb, cpu=cpu)
            ),
            ports=[ContainerPort(port=port)],
        )

        ip_address = IpAddress(
            type="Public",
            ports=[Port(port=port, protocol="TCP")],
            dns_name_label=dns_label,
        )

        group = ContainerGroup(
            location=region,
            containers=[container],
            os_type=OperatingSystemTypes.LINUX,
            restart_policy=ContainerGroupRestartPolicy.NEVER,
            ip_address=ip_address,
            image_registry_credentials=image_registry_creds or None,
            identity=identity,
        )

        try:
            poller = self.client.container_groups.begin_create_or_update(
                rg, group_name, group, polling_interval=5
            )
            created = poller.result(timeout=300)
        except HttpResponseError as e:
            raise ContainerException(f"ACI create failed: {e.message}")
        except Exception as e:
            raise ContainerException(f"ACI create failed: {e}")

        if created.ip_address is None or created.ip_address.fqdn is None:
            raise ContainerException("ACI container group has no public FQDN")

        return _CreatedContainer(
            id=group_name, hostname=created.ip_address.fqdn, port=port
        )

    def get_container_port(self, container_id: str):
        if self.client is None:
            return None
        try:
            cg = self.client.container_groups.get(
                self.settings["azure_resource_group"], container_id
            )
            if cg.containers and cg.containers[0].ports:
                return str(cg.containers[0].ports[0].port)
        except ResourceNotFoundError:
            return None
        except Exception as e:
            print(f"[CTFd-ACI] get_container_port({container_id}) failed: {e}")
            return None
        return None

    def get_container_hostname(self, container_id: str):
        if self.client is None:
            return None
        try:
            cg = self.client.container_groups.get(
                self.settings["azure_resource_group"], container_id
            )
            if cg.ip_address and cg.ip_address.fqdn:
                return cg.ip_address.fqdn
        except ResourceNotFoundError:
            return None
        except Exception as e:
            print(f"[CTFd-ACI] get_container_hostname({container_id}) failed: {e}")
            return None
        return None

    def get_images(self):
        login_server = self.settings.get("acr_login_server", "")
        if not login_server or self.credential is None:
            return []
        try:
            registry = ContainerRegistryClient(
                f"https://{login_server}", self.credential
            )
            images = []
            for repo in registry.list_repository_names():
                try:
                    count = 0
                    for tag in registry.list_tag_properties(
                        repo,
                        order_by=ArtifactTagOrder.LAST_UPDATED_ON_DESCENDING,
                    ):
                        images.append(f"{login_server}/{repo}:{tag.name}")
                        count += 1
                        if count >= MAX_TAGS_PER_REPO:
                            break
                except Exception as e:
                    print(f"[CTFd-ACI] list_tag_properties({repo}) failed: {e}")
                    continue
            images.sort()
            return images
        except Exception as e:
            print(f"[CTFd-ACI] get_images failed: {e}")
            return []

    def kill_container(self, container_id: str):
        if self.client is None:
            raise ContainerException("ACI client is not initialized")
        try:
            self.client.container_groups.begin_delete(
                self.settings["azure_resource_group"], container_id
            )
        except ResourceNotFoundError:
            pass
        except Exception as e:
            raise ContainerException(f"ACI delete failed: {e}")
