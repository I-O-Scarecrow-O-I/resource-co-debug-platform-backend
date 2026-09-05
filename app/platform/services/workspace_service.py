import json
import logging
import os
import stat
import threading
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from shutil import copytree, rmtree
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile

from app.core.errors import AppError, NotFoundError
from app.core.time import utc_now
from app.platform.domain.enums import ProjectStatus
from app.platform.domain.project import ProjectWorkspace

logger = logging.getLogger(__name__)


class WorkspaceService:
    _MANIFEST_FILENAME = ".project.json"
    _MANIFEST_VERSION = 1
    MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
    MULTIPART_OVERHEAD_BYTES = 1 * 1024 * 1024
    MAX_PROJECT_UPLOAD_BODY_BYTES = MAX_ARCHIVE_BYTES + MULTIPART_OVERHEAD_BYTES
    _MAX_ZIP_MEMBERS = 1_000
    _MAX_ZIP_FILE_BYTES = 10 * 1024 * 1024
    _MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024
    _MAX_ZIP_COMPRESSION_RATIO = 100
    _ZIP_CHUNK_BYTES = 64 * 1024

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._projects: dict[UUID, ProjectWorkspace] = {}
        self._lock = threading.RLock()
        self._load_projects()

    async def create_from_archive(
        self,
        archive: UploadFile,
        display_name: str | None = None,
    ) -> ProjectWorkspace:
        if not archive.filename:
            raise AppError("archive filename is required")
        if not archive.filename.lower().endswith(".zip"):
            raise AppError("only .zip project archives are supported in the first skeleton")

        project_id = uuid4()
        project_root = (self.storage_root / str(project_id)).resolve()
        upload_dir = project_root / "upload"
        source_dir = project_root / "source"
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            source_dir.mkdir(parents=True, exist_ok=True)

            archive_name = self._safe_filename(archive.filename)
            archive_path = upload_dir / archive_name
            with archive_path.open("wb") as target:
                self._copy_archive_limited(archive.file, target)

            self._extract_zip_safely(archive_path, source_dir)

            workspace = ProjectWorkspace(
                id=project_id,
                name=display_name or archive_name.removesuffix(".zip"),
                root_path=project_root,
                source_path=source_dir,
                status=ProjectStatus.READY,
                created_at=utc_now(),
            )
            self._write_manifest(workspace)
        except BadZipFile as exc:
            self._cleanup_failed_project(project_root)
            raise AppError("invalid zip archive") from exc
        except Exception:
            self._cleanup_failed_project(project_root)
            raise
        with self._lock:
            self._projects[project_id] = workspace
        return workspace

    def list_projects(self) -> list[ProjectWorkspace]:
        with self._lock:
            return sorted(self._projects.values(), key=lambda item: item.created_at, reverse=True)

    def require_project(self, project_id: UUID) -> ProjectWorkspace:
        with self._lock:
            project = self._projects.get(project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        return project

    def resolve_work_dir(self, project_id: UUID, work_dir: str | None) -> Path:
        project = self.require_project(project_id)
        return self.resolve_work_dir_in_workspace(project.source_path, work_dir)

    def resolve_work_dir_in_workspace(self, workspace: Path, work_dir: str | None) -> Path:
        source_root = workspace.resolve()
        requested = (source_root / (work_dir or ".")).resolve()
        if not requested.is_relative_to(source_root):
            raise AppError("work_dir must stay inside the project source directory")
        if not requested.is_dir():
            raise AppError(f"work_dir does not exist: {work_dir}")
        return requested

    def resolve_project_path(self, project_id: UUID, path: str) -> Path:
        project = self.require_project(project_id)
        return self.resolve_path_in_workspace(project.source_path, path)

    def resolve_path_in_workspace(self, workspace: Path, path: str) -> Path:
        source_root = workspace.resolve()
        requested_path = Path(path)
        requested = (
            requested_path if requested_path.is_absolute() else source_root / requested_path
        ).resolve()
        if not requested.is_relative_to(source_root):
            raise AppError("project path must stay inside the project source directory")
        return requested

    def create_task_workspace(
        self,
        project_id: UUID,
        task_id: UUID,
        source_path: Path | None = None,
        workspace_name: str | None = None,
    ) -> Path:
        project = self.require_project(project_id)
        task_root = project.root_path / "tasks" / str(task_id)
        workspace = task_root / (workspace_name or str(uuid4()))
        copytree(source_path or project.source_path, workspace)
        return workspace

    def resolve_task_workspace(self, project_id: UUID, task_id: UUID) -> Path:
        project = self.require_project(project_id)
        project_root = project.root_path
        tasks_root = project_root / "tasks"
        task_root = tasks_root / str(task_id)
        workspace = tasks_root / str(task_id) / "workspace"
        for path in (project_root, tasks_root, task_root, workspace):
            if self._is_link_or_reparse_point(path):
                raise AppError(f"unsafe build workspace: {task_id}")

        resolved_storage_root = self.storage_root.resolve()
        resolved_project_root = project_root.resolve()
        resolved_workspace = workspace.resolve()
        resolved_tasks_root = tasks_root.resolve()
        resolved_task_root = task_root.resolve()
        if not (
            resolved_project_root.is_relative_to(resolved_storage_root)
            and resolved_tasks_root.is_relative_to(resolved_project_root)
            and resolved_task_root.is_relative_to(resolved_tasks_root)
            and resolved_workspace.is_relative_to(resolved_task_root)
        ):
            raise AppError(f"unsafe build workspace: {task_id}")
        if not resolved_workspace.is_dir():
            raise AppError(f"build workspace does not exist: {task_id}")
        return resolved_workspace

    def list_task_artifacts(self, project_id: UUID, task_id: UUID) -> list[tuple[str, int]]:
        workspace = self.resolve_task_workspace(project_id, task_id)
        artifacts: list[tuple[str, int]] = []
        for current_root, directories, filenames in os.walk(workspace, followlinks=False):
            current_path = Path(current_root)
            directories[:] = [
                name
                for name in directories
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            for filename in filenames:
                path = current_path / filename
                if self._is_link_or_reparse_point(path):
                    continue
                try:
                    if not stat.S_ISREG(path.stat().st_mode):
                        continue
                    relative_path = path.relative_to(workspace).as_posix()
                    artifacts.append((relative_path, path.stat().st_size))
                except OSError:
                    continue
        return sorted(artifacts)

    def resolve_task_artifact(
        self,
        project_id: UUID,
        task_id: UUID,
        artifact_path: str,
    ) -> Path:
        workspace = self.resolve_task_workspace(project_id, task_id)
        requested_path = Path(artifact_path)
        windows_path = PureWindowsPath(artifact_path)
        if (
            not artifact_path
            or requested_path.is_absolute()
            or requested_path.drive
            or requested_path.root
            or requested_path.anchor
            or windows_path.drive
            or windows_path.root
            or windows_path.anchor
        ):
            raise AppError("artifact_path must be a non-empty relative path")

        candidate = workspace / requested_path
        resolved_candidate = candidate.resolve()
        if not resolved_candidate.is_relative_to(workspace):
            raise AppError("artifact_path must stay inside the build workspace")

        current = workspace
        for component in requested_path.parts:
            current /= component
            if self._is_link_or_reparse_point(current):
                raise AppError("artifact_path must not contain links or reparse points")

        if not candidate.exists():
            raise NotFoundError(f"artifact not found: {artifact_path}")
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise AppError("artifact_path must reference a regular file")
        return candidate

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        try:
            path_stat = path.lstat()
        except OSError:
            return False
        return stat.S_ISLNK(path_stat.st_mode) or bool(
            getattr(path_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def cleanup_task_workspaces(self, project_id: UUID, task_id: UUID) -> None:
        project = self.require_project(project_id)
        tasks_root = (project.root_path / "tasks").resolve()
        task_root = (tasks_root / str(task_id)).resolve()
        if not task_root.is_relative_to(tasks_root):
            raise AppError("invalid task workspace")
        if task_root.exists():
            rmtree(task_root)

    def _load_projects(self) -> None:
        for project_dir in self.storage_root.iterdir():
            try:
                if not project_dir.is_dir() or project_dir.is_symlink():
                    continue
                workspace = self._load_workspace(project_dir)
                if workspace is not None:
                    self._projects[workspace.id] = workspace
            except (OSError, RuntimeError):
                continue

    def _load_workspace(self, project_dir: Path) -> ProjectWorkspace | None:
        try:
            project_root = project_dir.resolve()
            if project_root.parent != self.storage_root:
                return None
            manifest = json.loads(
                (project_root / self._MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            if (
                not isinstance(manifest, dict)
                or type(manifest.get("version")) is not int
                or manifest["version"] != self._MANIFEST_VERSION
                or not isinstance(manifest.get("id"), str)
                or not isinstance(manifest.get("name"), str)
                or not isinstance(manifest.get("status"), str)
                or not isinstance(manifest.get("created_at"), str)
            ):
                return None

            project_id = UUID(manifest["id"])
            if project_root.name != str(project_id):
                return None
            status = ProjectStatus(manifest["status"])
            if status is not ProjectStatus.READY:
                return None
            created_at = datetime.fromisoformat(manifest["created_at"])
            if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
                return None

            source_dir = project_root / "source"
            if source_dir.is_symlink():
                return None
            source_path = source_dir.resolve()
            if (
                not source_path.is_relative_to(project_root)
                or not source_path.is_relative_to(self.storage_root)
                or not source_path.is_dir()
            ):
                return None
            for entry in source_path.rglob("*"):
                if entry.is_symlink() or not entry.resolve().is_relative_to(source_path):
                    return None
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            return None

        return ProjectWorkspace(
            id=project_id,
            name=manifest["name"],
            root_path=project_root,
            source_path=source_path,
            status=status,
            created_at=created_at,
        )

    def _write_manifest(self, workspace: ProjectWorkspace) -> None:
        manifest_path = workspace.root_path / self._MANIFEST_FILENAME
        temporary_path = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
        manifest = {
            "version": self._MANIFEST_VERSION,
            "id": str(workspace.id),
            "name": workspace.name,
            "status": workspace.status.value,
            "created_at": workspace.created_at.isoformat(),
        }
        try:
            temporary_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary_path.replace(manifest_path)
        except OSError:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    def _extract_zip_safely(self, archive_path: Path, source_dir: Path) -> None:
        source_root = source_dir.resolve()
        with ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > self._MAX_ZIP_MEMBERS:
                raise AppError("zip archive has too many entries")
            declared_total = 0
            for member in members:
                target = (source_root / member.filename).resolve()
                if not target.is_relative_to(source_root):
                    raise AppError(f"unsafe zip entry: {member.filename}")
                mode = member.external_attr >> 16
                entry_type = stat.S_IFMT(mode)
                if member.is_dir():
                    if entry_type not in {0, stat.S_IFDIR}:
                        raise AppError(f"unsupported zip entry: {member.filename}")
                    continue
                if entry_type not in {0, stat.S_IFREG}:
                    raise AppError(f"unsupported zip entry: {member.filename}")
                if member.file_size > self._MAX_ZIP_FILE_BYTES:
                    raise AppError(f"zip entry exceeds size limit: {member.filename}")
                declared_total += member.file_size
                if declared_total > self._MAX_ZIP_TOTAL_BYTES:
                    raise AppError("zip archive exceeds total size limit")
                if member.file_size and (
                    member.compress_size == 0
                    or member.file_size > member.compress_size * self._MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise AppError(f"zip entry exceeds compression ratio limit: {member.filename}")

            extracted_total = 0
            for member in members:
                target = (source_root / member.filename).resolve()
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted_size = 0
                with archive.open(member) as source, target.open("wb") as destination:
                    while chunk := source.read(self._ZIP_CHUNK_BYTES):
                        extracted_size += len(chunk)
                        extracted_total += len(chunk)
                        if (
                            extracted_size > self._MAX_ZIP_FILE_BYTES
                            or extracted_total > self._MAX_ZIP_TOTAL_BYTES
                        ):
                            raise AppError(f"zip entry exceeds size limit: {member.filename}")
                        destination.write(chunk)

    def _copy_archive_limited(self, source, destination) -> None:
        copied_bytes = 0
        while chunk := source.read(self._ZIP_CHUNK_BYTES):
            copied_bytes += len(chunk)
            if copied_bytes > self.MAX_ARCHIVE_BYTES:
                raise AppError("zip archive exceeds compressed size limit")
            destination.write(chunk)

    @staticmethod
    def _cleanup_failed_project(project_root: Path) -> None:
        try:
            rmtree(project_root)
        except OSError as exc:
            logger.warning("failed to remove project directory %s: %s", project_root, exc)

    def _safe_filename(self, filename: str) -> str:
        return "".join("_" if char in '\\/:*?"<>|' else char for char in filename)

