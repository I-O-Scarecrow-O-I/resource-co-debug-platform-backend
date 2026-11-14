import json
import logging
import os
import stat
import threading
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from shutil import copyfile, copytree, rmtree
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile

from app.core.errors import AppError, NotFoundError
from app.core.time import utc_now
from app.platform.domain.enums import ProjectStatus
from app.platform.domain.project import ProjectWorkspace

logger = logging.getLogger(__name__)


# 管理工程源树、任务副本和稳定扫描副本；这些目录有不同的写入及回收周期。
class WorkspaceService:
    _MANIFEST_FILENAME = ".project.json"
    _MANIFEST_VERSION = 1
    # 请求体限额额外容纳 multipart 开销，实际归档仍使用较低的压缩字节上限。
    MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
    MULTIPART_OVERHEAD_BYTES = 1 * 1024 * 1024
    MAX_PROJECT_UPLOAD_BODY_BYTES = MAX_ARCHIVE_BYTES + MULTIPART_OVERHEAD_BYTES
    # 条目数、单文件、累计解压量和压缩比共同限制归档消耗。
    _MAX_ZIP_MEMBERS = 5_000
    _MAX_ZIP_FILE_BYTES = 16 * 1024 * 1024
    _MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024
    _MAX_ZIP_COMPRESSION_RATIO = 100
    _ZIP_CHUNK_BYTES = 64 * 1024
    # 稳定扫描目录由项目持有，和按任务 ID 命名的副本分开。
    # 按任务回收时不会顺带删除供下一轮复用的扫描地址。
    _INCREMENTAL_WORKSPACE_NAME = "incremental_workspace"

    # 用解析后的存储根建立目录归属，启动时从清单恢复内存索引。
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._projects: dict[UUID, ProjectWorkspace] = {}
        self._lock = threading.RLock()
        self._load_projects()

    # 只有归档完整解包并成功写入清单后，才把工程加入可查询索引。
    async def create_from_archive(
        self,
        archive: UploadFile,
        display_name: str | None = None,
    ) -> ProjectWorkspace:
        # 归档入口只支持 ZIP。
        # 文件名校验用于早拒绝，实际格式还要由解包器验证。
        if not archive.filename:
            raise AppError("archive filename is required")
        if not archive.filename.lower().endswith(".zip"):
            raise AppError("only .zip project archives are supported in the first skeleton")

        # 工程路径由服务端 UUID 决定；原始压缩包和解出的工程树分开保存。
        project_id = uuid4()
        project_root = (self.storage_root / str(project_id)).resolve()
        upload_dir = project_root / "upload"
        source_dir = project_root / "source"
        # 上传阶段可以写压缩包和源目录，但此时工程未进入索引。
        # 如果解包或清单失败，只尝试回收本次生成的工程根。
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            source_dir.mkdir(parents=True, exist_ok=True)

            # 文件名只用于上传目录的归档展示，不能影响项目根目录选择。
            archive_name = self._safe_filename(archive.filename)
            archive_path = upload_dir / archive_name
            with archive_path.open("wb") as target:
                self._copy_archive_limited(archive.file, target)

            # 压缩字节通过检查后仍需独立检查解压条目。
            # 压缩包很小不能证明解开的目录同样小。
            self._extract_zip_safely(archive_path, source_dir)

            # 记录的工程名是展示属性，恢复和查找依赖 ID。
            # 原文件名或提交的名称相同也会得到独立工程。
            workspace = ProjectWorkspace(
                id=project_id,
                name=display_name or archive_name.removesuffix(".zip"),
                root_path=project_root,
                source_path=source_dir,
                status=ProjectStatus.READY,
                created_at=utc_now(),
            )
            # 先持久化 READY 清单，避免重启时恢复出未完整导入的工程。
            self._write_manifest(workspace)
        # 坏 ZIP 转为输入错误；无论失败类别，都尝试回收本次导入目录。
        except BadZipFile as exc:
            self._cleanup_failed_project(project_root)
            raise AppError("invalid zip archive") from exc
        except Exception:
            self._cleanup_failed_project(project_root)
            raise
        # 锁只用于最终发布索引，导入大归档期间不持有项目列表锁。
        with self._lock:
            self._projects[project_id] = workspace
        return workspace

    # 持锁生成按创建时间倒序的快照，调用者不访问服务内部索引。
    def list_projects(self) -> list[ProjectWorkspace]:
        with self._lock:
            return sorted(self._projects.values(), key=lambda item: item.created_at, reverse=True)

    # 所有基于工程 ID 的操作先取服务已认可的记录。
    # 不能仅凭目录名看起来像 UUID 就将其当成导入成功。
    def require_project(self, project_id: UUID) -> ProjectWorkspace:
        with self._lock:
            project = self._projects.get(project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        return project

    # 无工作区副本时在导入源树中解析，运行任务可调用下一层传入副本。
    def resolve_work_dir(self, project_id: UUID, work_dir: str | None) -> Path:
        # 选择源目录由调用入口决定；解析辅助函数只校验本次提供的根。
        # 不能用源树中的路径替代执行副本中对应的路径。
        project = self.require_project(project_id)
        return self.resolve_work_dir_in_workspace(project.source_path, work_dir)

    # 空工作目录代表副本根；解析后同时检查目录存在性和归属。
    def resolve_work_dir_in_workspace(self, workspace: Path, work_dir: str | None) -> Path:
        source_root = workspace.resolve()
        requested = (source_root / (work_dir or ".")).resolve()
        # 先消除路径跳转，再比较归属；只看字符串前缀会误认相邻工程。
        if not requested.is_relative_to(source_root):
            raise AppError("work_dir must stay inside the project source directory")
        if not requested.is_dir():
            raise AppError(f"work_dir does not exist: {work_dir}")
        return requested

    def resolve_project_path(self, project_id: UUID, path: str) -> Path:
        project = self.require_project(project_id)
        return self.resolve_path_in_workspace(project.source_path, path)

    # 接受已经展开的绝对路径，但其最终位置仍须位于指定工作区内。
    # 此方法不要求路径存在，调用者可以用它校验即将创建的文件。
    def resolve_path_in_workspace(self, workspace: Path, path: str) -> Path:
        source_root = workspace.resolve()
        requested_path = Path(path)
        requested = (
            requested_path if requested_path.is_absolute() else source_root / requested_path
        ).resolve()
        if not requested.is_relative_to(source_root):
            raise AppError("project path must stay inside the project source directory")
        return requested

    # 每次执行有独立副本；默认随机命名，需下载产物的任务可使用约定名称。
    # 源可来自已通过上层归属检查的上游任务副本。
    def create_task_workspace(
        self,
        project_id: UUID,
        task_id: UUID,
        source_path: Path | None = None,
        workspace_name: str | None = None,
        target_files: list[str] | None = None,
    ) -> Path:
        # 任务 ID 区分请求，副本名区分同一任务内部的运行。
        # 比较任务可以在一个任务根内创建多个互不共享写入的树。
        project = self.require_project(project_id)
        name = str(uuid4()) if workspace_name is None else workspace_name
        # 即使调用方给出固定名称，也要先通过组件校验，再参与路径拼接。
        self._validate_task_workspace_name(name)
        project_root = project.root_path.resolve()
        tasks_root = (project_root / "tasks").resolve()
        task_root = (tasks_root / str(task_id)).resolve()
        workspace = (task_root / name).resolve()
        # 逐级检查解析后的任务目录，防止父目录重定向把副本写到工程外。
        if not (
            tasks_root.is_relative_to(project_root)
            and task_root.is_relative_to(tasks_root)
            and workspace.is_relative_to(task_root)
        ):
            raise AppError("invalid task workspace path")
        # 使用新建目录复制，不将新源内容合并进已存在的固定副本。
        source = source_path or project.source_path
        if target_files is None:
            copytree(source, workspace)
        else:
            if (
                self._is_link_or_reparse_point(source)
                or not source.resolve().is_relative_to(project_root)
            ):
                raise AppError("invalid task workspace source")
            selected: list[tuple[Path, Path]] = []
            for relative_path in target_files:
                relative = Path(relative_path)
                if relative.is_absolute() or ".." in relative.parts:
                    raise AppError("target file must be relative to the workspace")
                current = source
                for component in relative.parts:
                    current /= component
                    if self._is_link_or_reparse_point(current):
                        raise AppError("target files must not contain links or reparse points")
                resolved = self.resolve_path_in_workspace(source, relative_path)
                if not resolved.is_file():
                    raise AppError(f"target file does not exist: {relative_path}")
                selected.append((resolved, workspace / relative))
            workspace.mkdir(parents=True)
            for original, target in selected:
                target.parent.mkdir(parents=True, exist_ok=True)
                copyfile(original, target)
        return workspace

    def sync_project_incremental_workspace(
        self,
        project_id: UUID,
        source_path: Path,
    ) -> Path:
        """Replace a project's stable scan tree with an exact copy of ``source_path``.

        The caller must serialize calls for the same project and keep that lock while
        the returned workspace is in use.
        """
        # 稳定扫描地址不允许引用工程之外的输入树。
        # 调用方持有该项目的扫描锁，直到不再使用此地址。
        project = self.require_project(project_id)
        project_root = project.root_path.resolve()
        source = source_path.resolve()
        # 输入必须是已存在的目录，且属于当前工程。
        # 仅有相同文件名的另一个工程副本不能成为增量源。
        if not source.is_dir() or not source.is_relative_to(project_root):
            raise AppError("incremental source must stay inside the project workspace")

        # 稳定地址用于上游按工作区身份复用增量信息。
        # 逐次换树时保留地址，并删除本次源中已经没有的旧文件。
        stable = (project_root / self._INCREMENTAL_WORKSPACE_NAME).resolve()
        if not stable.is_relative_to(project_root):
            raise AppError("invalid incremental workspace path")
        # 暂存与备份都位于同一工程根，复制完成后才移动既有稳定树。
        staging = (project_root / f".{self._INCREMENTAL_WORKSPACE_NAME}-{uuid4()}").resolve()
        backup = (project_root / f".{self._INCREMENTAL_WORKSPACE_NAME}-backup-{uuid4()}").resolve()
        if not staging.is_relative_to(project_root) or not backup.is_relative_to(project_root):
            raise AppError("invalid incremental workspace path")

        # 先完整构建新副本，减少旧扫描树不可用的时间。
        copytree(source, staging)
        moved_existing = False
        try:
            # 先把旧树让出稳定名称，备份仅在新树就位后丢弃。
            if stable.exists():
                stable.rename(backup)
                # 是否需要还原旧树由实际 rename 成功决定。
                # 没有移动过旧树时，失败分支不应尝试从备份恢复。
                moved_existing = True
            staging.rename(stable)
        # 切换失败时回滚原稳定树，避免下一次扫描指向半成品。
        except Exception:
            if staging.exists():
                rmtree(staging)
            if moved_existing and backup.exists() and not stable.exists():
                # 仅当稳定位置仍空着才把旧树移回来，避免在异常路径上覆写一个已经出现的树。
                backup.rename(stable)
            raise
        # 新树已就位时，备份删除失败只记录日志，不把成功切换当作失败。
        if moved_existing:
            try:
                rmtree(backup)
            except OSError:
                logger.warning("failed to remove incremental workspace backup %s", backup)
        return stable

    @staticmethod
    def _validate_task_workspace_name(workspace_name: str) -> None:
        # 名称只允许一个路径组件；同时排除两种平台上的盘符、分隔符和锚点。
        # 同时使用本机和 Windows 路径解析。
        # 单段名称在不同系统上也不能被解释为盘符或目录跳转。
        path = Path(workspace_name)
        windows_path = PureWindowsPath(workspace_name)
        if (
            not workspace_name
            or workspace_name in {".", ".."}
            or "/" in workspace_name
            or "\\" in workspace_name
            or ":" in workspace_name
            or path.is_absolute()
            or path.anchor
            or len(path.parts) != 1
            or windows_path.drive
            or windows_path.root
            or windows_path.anchor
        ):
            raise AppError("workspace_name must be a safe path component")

    # 只解析任务公开产物使用的固定副本，临时步骤目录不经此入口开放。
    def resolve_task_workspace(self, project_id: UUID, task_id: UUID) -> Path:
        project = self.require_project(project_id)
        project_root = project.root_path
        tasks_root = project_root / "tasks"
        task_root = tasks_root / str(task_id)
        workspace = tasks_root / str(task_id) / "workspace"
        # 在解析真实路径前拒绝任何层级的链接或重解析点。
        for path in (project_root, tasks_root, task_root, workspace):
            if self._is_link_or_reparse_point(path):
                raise AppError(f"unsafe build workspace: {task_id}")

        # 真实路径要在验证过的链条上逐级包含。
        # 固定副本即便名称正确，也不能脱离当前任务根。
        resolved_storage_root = self.storage_root.resolve()
        resolved_project_root = project_root.resolve()
        resolved_workspace = workspace.resolve()
        resolved_tasks_root = tasks_root.resolve()
        resolved_task_root = task_root.resolve()
        # 从存储根逐层确认归属，单独验证末级目录不足以保证项目隔离。
        if not (
            resolved_project_root.is_relative_to(resolved_storage_root)
            and resolved_tasks_root.is_relative_to(resolved_project_root)
            and resolved_task_root.is_relative_to(resolved_tasks_root)
            and resolved_workspace.is_relative_to(resolved_task_root)
        ):
            raise AppError(f"unsafe build workspace: {task_id}")
        # 格式与归属合法不保证副本仍存在。
        # 不存在的任务副本应在这里结束解析，不能作为空产物目录处理。
        if not resolved_workspace.is_dir():
            raise AppError(f"build workspace does not exist: {task_id}")
        return resolved_workspace

    # 列表只公布普通文件的相对路径和大小，不进入链接目录。
    def list_task_artifacts(self, project_id: UUID, task_id: UUID) -> list[tuple[str, int]]:
        workspace = self.resolve_task_workspace(project_id, task_id)
        artifacts: list[tuple[str, int]] = []
        # 枚举只返回快照，不承诺下一次下载时文件仍保持原大小。
        # 下载入口需要按当前磁盘状态重新确认。
        for current_root, directories, filenames in os.walk(workspace, followlinks=False):
            current_path = Path(current_root)
            # 在原列表上移除链接目录，通知 os.walk 不进入这些分支。
            directories[:] = [
                name
                for name in directories
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            # 任务输出可能含临时文件，遍历过程中不可访问的项目不阻断其他结果。
            for filename in filenames:
                path = current_path / filename
                if self._is_link_or_reparse_point(path):
                    continue
                try:
                    # 常规文件以相对路径对外展示，调用者不需要知道平台盘符。
                    # 遍历时文件可能消失，单条 stat 失败不丢掉整页结果。
                    if not stat.S_ISREG(path.stat().st_mode):
                        continue
                    relative_path = path.relative_to(workspace).as_posix()
                    artifacts.append((relative_path, path.stat().st_size))
                except OSError:
                    continue
        # 排序依据可对外使用的相对路径，而非平台相关的目录遍历顺序。
        return sorted(artifacts)

    # 下载会再次核验当前路径，不能仅依赖较早生成的产物列表。
    def resolve_task_artifact(
        self,
        project_id: UUID,
        task_id: UUID,
        artifact_path: str,
    ) -> Path:
        workspace = self.resolve_task_workspace(project_id, task_id)
        # 同时检查本机和 Windows 路径表示，避免不同平台对盘符或根的解释不同。
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

        # 归属检查和组件检查关注不同问题。
        # 前者禁止解析到外部，后者也禁止利用树内链接访问别名。
        candidate = workspace / requested_path
        resolved_candidate = candidate.resolve()
        if not resolved_candidate.is_relative_to(workspace):
            raise AppError("artifact_path must stay inside the build workspace")

        # 即使链接解析后仍在工作区内，也不允许借链接别名访问产物。
        current = workspace
        for component in requested_path.parts:
            current /= component
            if self._is_link_or_reparse_point(current):
                raise AppError("artifact_path must not contain links or reparse points")

        # 区分缺失产物与非普通文件；目录、设备等不作为文件流响应。
        if not candidate.exists():
            raise NotFoundError(f"artifact not found: {artifact_path}")
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise AppError("artifact_path must reference a regular file")
        return candidate

    @staticmethod
    # 用 lstat 查看链接本身；Windows 联接点也不能按普通目录处理。
    def _is_link_or_reparse_point(path: Path) -> bool:
        # 读取不了元数据时本辅助函数只表示未识别到链接。
        # 调用者还需要检查存在性、类型与解析后归属。
        try:
            path_stat = path.lstat()
        except OSError:
            return False
        # Windows 联接点可能不是 POSIX 意义上的符号链接。
        # 因此保留对重解析属性的独立判断。
        return stat.S_ISLNK(path_stat.st_mode) or bool(
            getattr(path_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    # 清理范围仅限指定任务的副本树，解析前后分别检查边界。
    def cleanup_task_workspaces(self, project_id: UUID, task_id: UUID) -> None:
        # 此处删除的是任务根，包含任务内部多个运行副本。
        # 持有状态在任务服务判断，文件系统层只负责安全定位和回收。
        with self._lock:
            project = self.require_project(project_id)
            project_root_path = project.root_path
            tasks_root_path = project_root_path / "tasks"
            task_root_path = tasks_root_path / str(task_id)
            for path in (project_root_path, tasks_root_path, task_root_path):
                if self._is_link_or_reparse_point(path):
                    raise AppError("invalid task workspace")

            project_root = project_root_path.resolve()
            # 清理时每层都重新解析，而非信任任务创建时解析过的路径。
            # 持久化任务 ID 并不能证明后来目录结构未被替换。
            tasks_root = tasks_root_path.resolve()
            task_root = task_root_path.resolve()
            if not (
                project_root.is_relative_to(self.storage_root)
                and tasks_root.is_relative_to(project_root)
                and task_root.is_relative_to(tasks_root)
            ):
                raise AppError("invalid task workspace")
            # 已经不存在的目录视作已清理；仍存在时保留底层失败供上层重试。
            if not task_root_path.exists():
                return
            # 目录在检查和删除之间可能消失。
            # 只在确认没有被替换成链接且确实不存在时接受该竞争。
            try:
                rmtree(task_root)
            except FileNotFoundError:
                if self._is_link_or_reparse_point(task_root_path):
                    raise AppError("invalid task workspace") from None
                if task_root_path.exists():
                    raise

    # 启动时从可信目录结构恢复工程，单个无效项目不阻断其他工程加载。
    def _load_projects(self) -> None:
        for project_dir in self.storage_root.iterdir():
            try:
                if not project_dir.is_dir() or project_dir.is_symlink():
                    continue
                workspace = self._load_workspace(project_dir)
                if workspace is not None:
                    self._projects[workspace.id] = workspace
            # 访问单个子目录失败时继续加载，避免一个遗留工程令其他项目不可用。
            # 加载器在这里不执行修复或删除。
            except (OSError, RuntimeError):
                continue

    # 版本、目录 ID、READY 状态和 UTC 时间共同定义可恢复的清单格式。
    def _load_workspace(self, project_dir: Path) -> ProjectWorkspace | None:
        try:
            # 恢复只接受存储根的一层项目目录。
            # 深层目录即便含有可读清单也不会成为独立工程。
            project_root = project_dir.resolve()
            if project_root.parent != self.storage_root:
                return None
            manifest = json.loads(
                (project_root / self._MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            # 版本使用真正的整数，避免布尔值在 Python 中与数字版本相等。
            # 标识、名称、状态和时间必须具有预期的 JSON 类型。
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

            # 清单身份要与磁盘 UUID 目录匹配。
            # 恢复后的查找不会因同名目录或错误拷贝指向别的工程。
            project_id = UUID(manifest["id"])
            if project_root.name != str(project_id):
                return None
            # 恢复只接纳已完成导入的 READY 状态。
            # 临时或失败目录即使留有可读 JSON 也不进入索引。
            status = ProjectStatus(manifest["status"])
            if status is not ProjectStatus.READY:
                return None
            # 要求带时区且偏移为零；恢复结果与新导入的 UTC 时间可直接排序。
            created_at = datetime.fromisoformat(manifest["created_at"])
            if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
                return None

            # 源树必须留在项目根下，恢复时还检查其内容没有链接逃逸。
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
            # 恢复的源树也可能被外部替换，需要重新验证其中的链接与路径。
            # 无效目录保持在磁盘上，由运维处理，不在加载时删除。
            for entry in source_path.rglob("*"):
                if entry.is_symlink() or not entry.resolve().is_relative_to(source_path):
                    return None
        # 清单类型、路径状态和时间解析的异常只使当前工程无效。
        # 后续请求不能依靠目录存在绕过清单认可。
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

    # 在同目录写临时清单，再替换正式文件，避免中途退出留下部分 JSON。
    def _write_manifest(self, workspace: ProjectWorkspace) -> None:
        manifest_path = workspace.root_path / self._MANIFEST_FILENAME
        temporary_path = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
        # 清单仅存可恢复身份和状态；根路径根据受控目录结构重新计算。
        # 迁移存储根时不依赖旧机器的绝对路径字符串。
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
            # 只有临时文件完整写入后才替换正式清单。
            # 这避免读到半份 JSON，但不把清单操作等同于整个导入事务。
            temporary_path.replace(manifest_path)
        # 尽量移除临时文件并保留原写入异常，让导入流程决定整体回收。
        except OSError:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    # 先完整预检所有条目，再打开目的文件，避免明显无效的归档解包到一半。
    def _extract_zip_safely(self, archive_path: Path, source_dir: Path) -> None:
        # 解包的根目录来自本次导入；归档条目只能相对此根创建文件和目录。
        source_root = source_dir.resolve()
        with ZipFile(archive_path) as archive:
            # 先审阅整个中央目录，再创建任何条目；元数据检查失败时不进入解包阶段。
            members = archive.infolist()
            # 成员数限制也覆盖目录条目，避免大量小路径占用处理时间。
            if len(members) > self._MAX_ZIP_MEMBERS:
                raise AppError("zip archive has too many entries")
            # 声明总量用于提前拒绝危险归档；它不是后面写入字节的累计器。
            declared_total = 0
            for member in members:
                # 按归一化路径检查解包目的地；外部属性仅允许普通文件与目录。
                target = (source_root / member.filename).resolve()
                if not target.is_relative_to(source_root):
                    raise AppError(f"unsafe zip entry: {member.filename}")
                # 条目类型由归档名与 UNIX 类型位共同判断。
                # 不采用压缩包携带的可执行权限或链接类型。
                mode = member.external_attr >> 16
                # 目录标记与 Unix 文件类型必须相容；没有类型位时允许按目录标记识别。
                entry_type = stat.S_IFMT(mode)
                # 目录条目不走普通文件的尺寸或压缩比检查。
                # 它已经受归属、类型和条目数量约束。
                if member.is_dir():
                    if entry_type not in {0, stat.S_IFDIR}:
                        raise AppError(f"unsupported zip entry: {member.filename}")
                    continue
                if entry_type not in {0, stat.S_IFREG}:
                    raise AppError(f"unsupported zip entry: {member.filename}")
                # 声明大小用于预筛，但不能替代写入阶段的真实字节计数。
                if member.file_size > self._MAX_ZIP_FILE_BYTES:
                    raise AppError(f"zip entry exceeds size limit: {member.filename}")
                # 每个普通文件计入声明总量。
                # 目录不计展开字节，但已经计入成员数限制。
                declared_total += member.file_size
                # 多个各自合格的小文件合计也可能超限，因此累计预算在遍历中持续检查。
                if declared_total > self._MAX_ZIP_TOTAL_BYTES:
                    raise AppError("zip archive exceeds total size limit")
                # 空文件可以没有压缩数据。
                # 非空文件如果没有合理压缩大小则不能通过压缩比检查。
                if member.file_size and (
                    member.compress_size == 0
                    or member.file_size > member.compress_size * self._MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise AppError(f"zip entry exceeds compression ratio limit: {member.filename}")

            # 第二遍按块读写，每块都先计入单文件与总预算，再落盘。
            extracted_total = 0
            # 完成整份中央目录的检查后才创建条目。
            # 逐文件再次计算落点，以实际源根为目录边界。
            for member in members:
                target = (source_root / member.filename).resolve()
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                # 普通文件创建前确保父目录存在。
                # 归档不必为每一层目录都提供独立条目。
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted_size = 0
                # 以小块读取成员，避免将整个解压文件驻留内存。
                # 读出当前块后先核对累计量，再允许向目标写入。
                with archive.open(member) as source, target.open("wb") as destination:
                    while chunk := source.read(self._ZIP_CHUNK_BYTES):
                        # 当前块同时占用本文件和整个归档的额度。
                        # 每个新文件重置前者，后者从首个成员累加到最后一个。
                        extracted_size += len(chunk)
                        extracted_total += len(chunk)
                        # 流中实际读取量可能不同于声明。
                        # 超限块先拒绝，不把它写进部分解压的结果树。
                        if (
                            extracted_size > self._MAX_ZIP_FILE_BYTES
                            or extracted_total > self._MAX_ZIP_TOTAL_BYTES
                        ):
                            raise AppError(f"zip entry exceeds size limit: {member.filename}")
                        destination.write(chunk)

    # 限制实际读取的压缩流，不能信任客户端上报的上传长度。
    def _copy_archive_limited(self, source, destination) -> None:
        # 上传原始字节与解压后字节是独立配额；压缩包本身太大时还没有进入 ZIP 解析。
        copied_bytes = 0
        while chunk := source.read(self._ZIP_CHUNK_BYTES):
            copied_bytes += len(chunk)
            # 压缩流逐块判限，错误由导入入口回收部分写入的上传文件。
            if copied_bytes > self.MAX_ARCHIVE_BYTES:
                raise AppError("zip archive exceeds compressed size limit")
            destination.write(chunk)

    @staticmethod
    # 清理失败保留日志，主异常仍能作为导入失败的原因返回。
    def _cleanup_failed_project(project_root: Path) -> None:
        try:
            # 清理失败只写诊断；调用者仍应看到导致这次导入失败的原始异常。
            rmtree(project_root)
        except OSError as exc:
            logger.warning("failed to remove project directory %s: %s", project_root, exc)

    # 保留用户文件名的可读部分，同时替换常见路径分隔符和 Windows 保留字符。
    def _safe_filename(self, filename: str) -> str:
        return "".join("_" if char in '\\/:*?"<>|' else char for char in filename)
