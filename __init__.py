from __future__ import division

import time
import json
import datetime
import math
import threading

from flask import Blueprint, request, Flask, render_template, url_for, redirect, flash
from sqlalchemy.exc import IntegrityError

from CTFd.models import db, Solves
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge
from CTFd.utils.decorators import authed_only, admins_only, during_ctf_time_only, ratelimit, require_verified_emails
from CTFd.utils.user import get_current_user
from CTFd.utils.modes import get_model

from .models import ContainerChallengeModel, ContainerInfoModel, ContainerSettingsModel, resolve_size
from .container_manager import ContainerManager, ContainerException
from .container_manager_aci import ACIContainerManager


def make_container_manager(settings, app):
    if settings.get("backend", "docker") == "aci":
        return ACIContainerManager(settings, app)
    return ContainerManager(settings, app)


class ContainerChallenge(BaseChallenge):
    id = "container"  # Unique identifier used to register challenges
    name = "container"  # Name of a challenge type
    templates = {  # Handlebars templates used for each aspect of challenge editing & viewing
        "create": "/plugins/containers/assets/create.html",
        "update": "/plugins/containers/assets/update.html",
        "view": "/plugins/containers/assets/view.html",
    }
    # Scripts loaded by CTFd's challenge-type editor. Plain paths only —
    # appending a `?v=<hash>` cache-buster here breaks the load because CTFd
    # URL-encodes the value, so the `?` ends up as `%3F` in the request path
    # and Flask's static route returns 404. Hard-refresh after a plugin
    # update if your browser is caching a stale copy.
    scripts = {
        "create": "/plugins/containers/assets/create.js",
        "update": "/plugins/containers/assets/update.js",
        "view": "/plugins/containers/assets/view.js",
    }
    # Route at which files are accessible. This must be registered using register_plugin_assets_directory()
    route = "/plugins/containers/assets/"

    challenge_model = ContainerChallengeModel

    @classmethod
    def read(cls, challenge):
        """
        This method is in used to access the data of a challenge in a format processable by the front end.

        :param challenge:
        :return: Challenge object, data dictionary to be returned to the user
        """
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "image": challenge.image,
            "port": challenge.port,
            "command": challenge.command,
            "size": challenge.size,
            "initial": challenge.initial,
            "decay": challenge.decay,
            "minimum": challenge.minimum,
            "description": challenge.description,
            "connection_info": challenge.connection_info,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data

    @classmethod
    def calculate_value(cls, challenge):
        # No decay curve configured — Static scoring. CTFd 3.8.x's unified
        # Scoring Function selector submits `value` directly for Static
        # challenges (the legacy `initial` column isn't editable from that UI),
        # so trust whatever the form just set and don't clobber it with the
        # stale `initial` from when the challenge was first created. We still
        # need to commit so the setattr() changes from update() persist —
        # CTFd's PATCH wrapper doesn't commit on its own.
        if not challenge.decay:
            db.session.commit()
            return challenge

        Model = get_model()

        solve_count = (
            Solves.query.join(Model, Solves.account_id == Model.id)
            .filter(
                Solves.challenge_id == challenge.id,
                Model.hidden == False,
                Model.banned == False,
            )
            .count()
        )

        # If the solve count is 0 we shouldn't manipulate the solve count to
        # let the math update back to normal
        if solve_count != 0:
            # We subtract -1 to allow the first solver to get max point value
            solve_count -= 1

        # It is important that this calculation takes into account floats.
        # Hence this file uses from __future__ import division
        value = (
            ((challenge.minimum - challenge.initial) / (challenge.decay ** 2))
            * (solve_count ** 2)
        ) + challenge.initial

        value = math.ceil(value)

        if value < challenge.minimum:
            value = challenge.minimum

        challenge.value = value
        db.session.commit()
        return challenge

    @classmethod
    def update(cls, challenge, request):
        """
        This method is used to update the information associated with a challenge. This should be kept strictly to the
        Challenges table and any child tables.
        :param challenge:
        :param request:
        :return:
        """
        data = request.form or request.get_json()

        for attr, value in data.items():
            # We need to set these to floats so that the next operations don't operate on strings
            if attr in ("initial", "minimum", "decay"):
                value = float(value)
            setattr(challenge, attr, value)

        return ContainerChallenge.calculate_value(challenge)

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)

        ContainerChallenge.calculate_value(challenge)

        # Tear down the player's container for this challenge — it's served
        # its purpose and we don't want to keep paying ACI for it.
        manager = getattr(cls, "container_manager", None)
        if manager is not None and user is not None:
            info = ContainerInfoModel.query.filter_by(
                challenge_id=challenge.id, user_id=user.id).first()
            if info is not None:
                if info.container_id:
                    try:
                        manager.kill_container(info.container_id)
                    except ContainerException as e:
                        print(f"[CTFd] solve cleanup kill failed for {info.container_id}: {e}")
                db.session.delete(info)
                db.session.commit()


def settings_to_dict(settings):
    return {
        setting.key: setting.value for setting in settings
    }


def _ensure_size_column():
    """Add the per-challenge `size` column on installs that predate it.

    `db.create_all()` creates missing tables but never ALTERs existing ones, so
    upgrading an instance that already has challenges needs this. The ADD COLUMN
    ... DEFAULT 'small' also backfills existing rows on MySQL/MariaDB and SQLite,
    so legacy challenges keep their current 1 vCPU / 1.5 GB behavior. Idempotent.
    """
    from sqlalchemy import inspect as sa_inspect, text

    table = ContainerChallengeModel.__table__.name
    try:
        columns = [c["name"] for c in sa_inspect(db.engine).get_columns(table)]
    except Exception as e:
        print(f"[CTFd] could not inspect {table} for `size` column: {e}")
        return
    if "size" in columns:
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN size VARCHAR(16) DEFAULT 'small'")
            )
        print(f"[CTFd] added `size` column to {table}")
    except Exception as e:
        print(f"[CTFd] failed to add `size` column to {table}: {e}")


def load(app: Flask):
    app.db.create_all()
    _ensure_size_column()
    CHALLENGE_CLASSES["container"] = ContainerChallenge
    register_plugin_assets_directory(
        app, base_path="/plugins/containers/assets/"
    )

    container_settings = settings_to_dict(ContainerSettingsModel.query.all())
    container_manager = make_container_manager(container_settings, app)
    # Make the manager available to ContainerChallenge classmethods (e.g. solve()).
    ContainerChallenge.container_manager = container_manager

    containers_bp = Blueprint(
        'containers', __name__, template_folder='templates', static_folder='assets', url_prefix='/containers')

    @containers_bp.app_template_filter("format_time")
    def format_time_filter(unix_seconds):
        # return time.ctime(unix_seconds)
        return datetime.datetime.fromtimestamp(unix_seconds, tz=datetime.datetime.now(
            datetime.timezone.utc).astimezone().tzinfo).isoformat()

    def kill_container(container_id):
        container: ContainerInfoModel = ContainerInfoModel.query.filter_by(
            container_id=container_id).first()

        try:
            container_manager.kill_container(container_id)
        except ContainerException:
            return {"error": "Docker is not initialized. Please check your settings."}

        if container is not None:
            db.session.delete(container)
            db.session.commit()
        return {"success": "Container killed"}

    def renew_container(chal_id, user_id):
        # Get the requested challenge
        challenge = ContainerChallenge.challenge_model.query.filter_by(
            id=chal_id).first()

        # Make sure the challenge exists and is a container challenge
        if challenge is None:
            return {"error": "Challenge not found"}, 400

        running_containers = ContainerInfoModel.query.filter_by(
            challenge_id=challenge.id, user_id=user_id)
        running_container = running_containers.first()

        if running_container is None:
            return {"error": "Container not found, try resetting the container."}

        try:
            running_container.expires = int(
                time.time() + container_manager.expiration_seconds)
            db.session.commit()
        except ContainerException:
            return {"error": "Database error occurred, please try again."}

        return {"success": "Container renewed", "expires": running_container.expires}

    def _running_response(row: ContainerInfoModel):
        return {
            "status": "running",
            "id": row.id,
            "hostname": row.hostname or container_manager.settings.get("docker_hostname", ""),
            "port": row.port,
            "expires": row.expires,
        }

    def _provision_async(manager, row_id, image, internal_port, command, volumes, expiration_seconds, owner=None, cpu=None, memory=None):
        with app.app_context():
            if ContainerInfoModel.query.get(row_id) is None:
                return
            try:
                created = manager.create_container(image, internal_port, command, volumes, owner=owner, cpu=cpu, memory=memory)
            except ContainerException as e:
                row = ContainerInfoModel.query.get(row_id)
                if row is not None:
                    row.status = "failed"
                    row.error_message = str(e)[:1000]
                    db.session.commit()
                print(f"[CTFd] provision failed for row {row_id}: {e}")
                return
            except Exception as e:
                row = ContainerInfoModel.query.get(row_id)
                if row is not None:
                    row.status = "failed"
                    row.error_message = str(e)[:1000]
                    db.session.commit()
                print(f"[CTFd] provision exception for row {row_id}: {e}")
                return

            # Re-fetch in case the user stopped/reset the request while we were
            # blocked on the backend. If the row is gone, the user no longer
            # wants this container — kill it so we don't leak (and pay for) it.
            row = ContainerInfoModel.query.get(row_id)
            if row is None:
                try:
                    manager.kill_container(created.id)
                except Exception as e:
                    print(f"[CTFd] orphan cleanup failed for {created.id}: {e}")
                return

            row.container_id = created.id
            row.hostname = getattr(created, "hostname", None)
            host_port = None
            try:
                host_port = manager.get_container_port(created.id)
            except Exception as e:
                print(f"[CTFd] get_container_port failed for {created.id}: {e}")
            if host_port is None:
                row.port = internal_port
            else:
                try:
                    row.port = int(host_port)
                except (TypeError, ValueError):
                    row.port = internal_port
            if expiration_seconds > 0:
                row.expires = int(time.time() + expiration_seconds)
            row.status = "running"
            row.error_message = None
            db.session.commit()

    def _spawn_new(challenge, user_id, user_name=None):
        now = int(time.time())
        initial_expires = now + (container_manager.expiration_seconds or 3600)
        row = ContainerInfoModel(
            challenge_id=challenge.id,
            user_id=user_id,
            status="provisioning",
            timestamp=now,
            expires=initial_expires,
        )
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = ContainerInfoModel.query.filter_by(
                challenge_id=challenge.id, user_id=user_id).first()
            if existing:
                if existing.status == "running":
                    return _running_response(existing), 200
                return {"status": existing.status, "id": existing.id}, 202
            return {"error": "Concurrent request collision"}, 500

        cpu, memory_mb = resolve_size(getattr(challenge, "size", None))
        threading.Thread(
            target=_provision_async,
            args=(container_manager, row.id, challenge.image, challenge.port,
                  challenge.command, challenge.volumes,
                  container_manager.expiration_seconds),
            kwargs={"owner": user_name, "cpu": cpu, "memory": memory_mb},
            daemon=True,
        ).start()

        return {"status": "provisioning", "id": row.id}, 202

    @containers_bp.route('/api/request', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=6, interval=60)
    def route_request_container():
        user = get_current_user()

        if request.json is None:
            return {"error": "Invalid request"}, 400
        chal_id = request.json.get("chal_id")
        if chal_id is None:
            return {"error": "No chal_id specified"}, 400
        if user is None:
            return {"error": "User not found"}, 400

        challenge = ContainerChallenge.challenge_model.query.filter_by(id=chal_id).first()
        if challenge is None:
            return {"error": "Challenge not found"}, 400

        existing = ContainerInfoModel.query.filter_by(
            challenge_id=chal_id, user_id=user.id).first()

        if existing:
            if existing.status == "running":
                try:
                    if container_manager.is_container_running(existing.container_id):
                        return _running_response(existing), 200
                    db.session.delete(existing)
                    db.session.commit()
                except ContainerException as err:
                    return {"error": str(err)}, 500
            elif existing.status == "provisioning":
                return {"status": "provisioning", "id": existing.id}, 202
            elif existing.status == "failed":
                # Allow retry by clearing the failed row.
                db.session.delete(existing)
                db.session.commit()

        return _spawn_new(challenge, user.id, user_name=user.name)

    @containers_bp.route('/api/status/<int:row_id>', methods=['GET'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="GET", limit=120, interval=60)
    def route_container_status(row_id):
        user = get_current_user()
        if user is None:
            return {"error": "User not found"}, 400
        row = ContainerInfoModel.query.get(row_id)
        if row is None:
            return {"error": "Not found"}, 404
        if row.user_id != user.id:
            return {"error": "Forbidden"}, 403
        if row.status == "running":
            return _running_response(row), 200
        if row.status == "failed":
            return {"status": "failed", "error": row.error_message or "Provisioning failed"}, 200
        return {"status": "provisioning", "id": row.id}, 202

    @containers_bp.route('/api/running/<int:chal_id>', methods=['GET'])
    @authed_only
    @ratelimit(method="GET", limit=120, interval=60)
    def route_running_container(chal_id):
        user = get_current_user()
        if user is None:
            return {"error": "User not found"}, 400
        row = ContainerInfoModel.query.filter_by(
            challenge_id=chal_id, user_id=user.id).first()
        if row is None:
            return {"status": "none"}, 200
        if row.status == "running":
            try:
                if container_manager.is_container_running(row.container_id):
                    return _running_response(row), 200
            except ContainerException:
                pass
            # Stale row — backend reports container is gone. Clean it up
            # so the next request can spawn a fresh one.
            db.session.delete(row)
            db.session.commit()
            return {"status": "none"}, 200
        if row.status == "provisioning":
            return {"status": "provisioning", "id": row.id}, 202
        if row.status == "failed":
            return {"status": "failed", "error": row.error_message or "Provisioning failed"}, 200
        return {"status": "none"}, 200

    @containers_bp.route('/api/renew', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=6, interval=60)
    def route_renew_container():
        user = get_current_user()

        # Validate the request
        if request.json is None:
            return {"error": "Invalid request"}, 400

        if request.json.get("chal_id", None) is None:
            return {"error": "No chal_id specified"}, 400

        if user is None:
            return {"error": "User not found"}, 400

        try:
            return renew_container(request.json.get("chal_id"), user.id)
        except ContainerException as err:
            return {"error": str(err)}, 500

    @containers_bp.route('/api/reset', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=6, interval=60)
    def route_restart_container():
        user = get_current_user()

        if request.json is None:
            return {"error": "Invalid request"}, 400
        chal_id = request.json.get("chal_id")
        if chal_id is None:
            return {"error": "No chal_id specified"}, 400
        if user is None:
            return {"error": "User not found"}, 400

        challenge = ContainerChallenge.challenge_model.query.filter_by(id=chal_id).first()
        if challenge is None:
            return {"error": "Challenge not found"}, 400

        existing = ContainerInfoModel.query.filter_by(
            challenge_id=chal_id, user_id=user.id).first()

        if existing:
            if existing.container_id:
                try:
                    container_manager.kill_container(existing.container_id)
                except ContainerException as err:
                    print(f"[CTFd] reset: kill_container({existing.container_id}) failed: {err}")
            db.session.delete(existing)
            db.session.commit()

        return _spawn_new(challenge, user.id, user_name=user.name)

    @containers_bp.route('/api/stop', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=10, interval=60)
    def route_stop_container():
        user = get_current_user()

        # Validate the request
        if request.json is None:
            return {"error": "Invalid request"}, 400

        if request.json.get("chal_id", None) is None:
            return {"error": "No chal_id specified"}, 400

        if user is None:
            return {"error": "User not found"}, 400

        row: ContainerInfoModel = ContainerInfoModel.query.filter_by(
            challenge_id=request.json.get("chal_id"), user_id=user.id).first()

        if row is None:
            return {"error": "No container found"}, 400
        if row.container_id:
            try:
                container_manager.kill_container(row.container_id)
            except ContainerException as err:
                return {"error": str(err)}, 500
        db.session.delete(row)
        db.session.commit()
        return {"success": "Container killed"}

    @containers_bp.route('/api/kill', methods=['POST'])
    @admins_only
    def route_kill_container():
        if request.json is None:
            return {"error": "Invalid request"}, 400

        # Accept either the new `id` (row id) or legacy `container_id` (backend name).
        row_id = request.json.get("id")
        container_id = request.json.get("container_id")
        row = None
        if row_id is not None:
            try:
                row = ContainerInfoModel.query.get(int(row_id))
            except (ValueError, TypeError):
                row = None
        elif container_id is not None:
            row = ContainerInfoModel.query.filter_by(container_id=container_id).first()
        else:
            return {"error": "No id or container_id specified"}, 400

        if row is None:
            return {"error": "Not found"}, 404
        if row.container_id:
            try:
                container_manager.kill_container(row.container_id)
            except ContainerException as err:
                print(f"[CTFd] admin kill failed: {err}")
        db.session.delete(row)
        db.session.commit()
        return {"success": "Container killed"}

    @containers_bp.route('/api/purge', methods=['POST'])
    @admins_only
    def route_purge_containers():
        containers: "list[ContainerInfoModel]" = ContainerInfoModel.query.all()
        for container in containers:
            if container.container_id:
                try:
                    container_manager.kill_container(container.container_id)
                except ContainerException as err:
                    print(f"[CTFd] purge: kill_container({container.container_id}) failed: {err}")
            db.session.delete(container)
        db.session.commit()
        return {"success": "Purged all containers"}, 200

    @containers_bp.route('/api/images', methods=['GET'])
    @admins_only
    def route_get_images():
        try:
            images = container_manager.get_images()
        except ContainerException as err:
            return {"error": str(err)}

        return {"images": images}

    @containers_bp.route('/api/settings/update', methods=['POST'])
    @admins_only
    def route_update_settings():
        nonlocal container_manager

        settable_keys = (
            "backend",
            "docker_base_url",
            "docker_hostname",
            "container_expiration",
            "container_maxmemory",
            "container_maxcpu",
            "azure_subscription_id",
            "azure_resource_group",
            "azure_region",
            "azure_uami_resource_id",
            "azure_dns_label_prefix",
            "acr_login_server",
        )

        for key in settable_keys:
            value = request.form.get(key)
            if value is None:
                continue
            row = ContainerSettingsModel.query.filter_by(key=key).first()
            if row is None:
                db.session.add(ContainerSettingsModel(key=key, value=value))
            else:
                row.value = value

        db.session.commit()

        # Cleanly shut down the previous manager (stops its expiration scheduler)
        # before replacing it, so we don't leak duplicate sweepers on every save.
        try:
            container_manager.shutdown()
        except Exception as err:
            print(f"[CTFd] previous manager shutdown failed: {err}")

        new_settings = settings_to_dict(ContainerSettingsModel.query.all())
        container_manager = make_container_manager(new_settings, app)

        try:
            container_manager.initialize_connection(new_settings, app)
        except ContainerException as err:
            flash(str(err), "error")
            return redirect(url_for(".route_containers_settings"))

        return redirect(url_for(".route_containers_dashboard"))

    @containers_bp.route('/dashboard', methods=['GET'])
    @admins_only
    def route_containers_dashboard():
        running_containers = ContainerInfoModel.query.order_by(
            ContainerInfoModel.timestamp.desc()).all()

        connected = False
        try:
            connected = container_manager.is_connected()
        except ContainerException:
            pass

        for i, container in enumerate(running_containers):
            try:
                running_containers[i].is_running = container_manager.is_container_running(
                    container.container_id)
            except ContainerException:
                running_containers[i].is_running = False

        backend = container_manager.settings.get("backend", "docker")
        backend_label = "Azure Container Instances" if backend == "aci" else "Docker"

        return render_template(
            'container_dashboard.html',
            containers=running_containers,
            connected=connected,
            backend=backend,
            backend_label=backend_label,
        )

    @containers_bp.route('/settings', methods=['GET'])
    @admins_only
    def route_containers_settings():
        running_containers = ContainerInfoModel.query.order_by(
            ContainerInfoModel.timestamp.desc()).all()
        return render_template('container_settings.html', settings=container_manager.settings)

    app.register_blueprint(containers_bp)
