"""Docker sandbox backend implementation."""

from __future__ import annotations

import io
import posixpath
import shlex
import tarfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

if TYPE_CHECKING:
    from docker.models.containers import Container


def _validate_sandbox_path(path: str) -> str:
    """Validate and normalize a sandbox file path.

    Prevents path traversal attacks by rejecting paths with '..' components
    and requiring absolute paths.

    Args:
        path: File path inside the sandbox

    Returns:
        Normalized absolute path

    Raises:
        ValueError: If path contains traversal sequences or is not absolute
    """
    normalized = posixpath.normpath(path)
    if ".." in normalized.split("/"):
        msg = f"Path traversal not allowed: {path}"
        raise ValueError(msg)
    if not normalized.startswith("/"):
        msg = f"Absolute path required in sandbox: {path}"
        raise ValueError(msg)
    return normalized


# Environment injected into every executed command. These make the sandbox
# behave non-interactively and predictably so the agent's commands don't hang
# waiting for input and so output decodes cleanly:
#   - GIT_TERMINAL_PROMPT=0: git fails fast instead of blocking on a credential
#     prompt (the classic sandbox hang on a private `git clone`).
#   - DEBIAN_FRONTEND / CI: apt and many CLIs skip interactive prompts.
#   - PYTHON*: unbuffered, UTF-8 I/O so output streams promptly and decodes.
#   - PIP_*: quieter, root-safe, non-interactive pip inside the container.
#   - TERM=dumb: suppress ANSI control sequences that would pollute captured output.
#   - LANG/LC_ALL: a UTF-8 locale so tools emit UTF-8 rather than ASCII-mangled bytes.
# PATH is intentionally NOT set here — overriding it would break custom images
# that put interpreters on a non-default PATH (venvs, nvm, /opt/...).
_DEFAULT_EXEC_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "DEBIAN_FRONTEND": "noninteractive",
    "CI": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_ROOT_USER_ACTION": "ignore",
    "PIP_NO_INPUT": "1",
    "TERM": "dumb",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class DockerBackend(BaseSandbox):
    """Docker backend implementation conforming to SandboxBackendProtocol.

    This implementation inherits all file operation methods from BaseSandbox
    and implements execute(), download_files(), and upload_files() using Docker's API.
    """

    def __init__(self, container: Container, workdir: str = "/workspace") -> None:
        """Initialize the DockerBackend with a Docker container instance.

        Args:
            container: Active Docker Container instance
            workdir: Working directory for executed commands. Matches the
                container's configured working_dir (and the bind-mount target
                when the project is mounted), so shell commands run against the
                mounted project rather than the container root.
        """
        self._container = container
        self._workdir = workdir
        self._timeout = 30 * 60  # 30 minutes default timeout

    @property
    def id(self) -> str:
        """Unique identifier for the sandbox backend."""
        return self._container.id[:12]  # Use short container ID

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a command in the Docker container and return ExecuteResponse.

        Args:
            command: Full shell command string to execute
            timeout: Maximum time in seconds to wait for the command to complete.
                If None, uses the backend's default timeout (30 minutes).

        Returns:
            ExecuteResponse with combined output, exit code, and truncation flag
        """
        try:
            # Docker SDK's Container.exec_run() does NOT support a timeout
            # parameter. Run the command under coreutils' timeout so the
            # container-side process is stopped too; the thread deadline below
            # is only a guard against a wedged Docker API call.
            timeout_sec = timeout if timeout is not None else self._timeout
            timeout_command = (
                f"timeout --signal=TERM --kill-after=2s {max(timeout_sec, 0.001)}s "
                f"bash -c {shlex.quote(command)}"
            )
            pool = ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(
                self._container.exec_run,
                cmd=["bash", "-c", timeout_command],
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                demux=False,  # Combine stdout and stderr
                workdir=self._workdir,
                environment=_DEFAULT_EXEC_ENV,
            )
            try:
                exec_result = fut.result(timeout=max(timeout_sec, 0.001) + 5)
            except FuturesTimeout:
                # A blocked SDK call must not make executor shutdown wait
                # for its worker. The worker unwinds when the API call returns.
                pool.shutdown(wait=False, cancel_futures=True)
                return ExecuteResponse(
                    output=f"Command timed out after {timeout_sec}s: {command[:200]}",
                    exit_code=124,  # Standard timeout exit code
                    truncated=False,
                )
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)

            # Decode output. Use errors="replace" so a command that emits any
            # non-UTF-8 byte (binary output, latin-1 logs) still returns its real
            # output instead of crashing the whole execute() with UnicodeDecodeError.
            output = (
                exec_result.output.decode("utf-8", errors="replace") if exec_result.output else ""
            )

            return ExecuteResponse(
                output=output,
                exit_code=exec_result.exit_code,
                truncated=False,  # Docker doesn't provide truncation info
            )

        except Exception as e:
            # Return error as output
            return ExecuteResponse(
                output=f"Error executing command: {e}",
                exit_code=1,
                truncated=False,
            )

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the Docker container.

        Uses Docker's get_archive() API to retrieve files.

        Args:
            paths: List of file paths to download

        Returns:
            List of FileDownloadResponse objects, one per input path
        """
        responses = []

        for path in paths:
            try:
                path = _validate_sandbox_path(path)  # noqa: PLW2901

                # Get archive from container
                bits, stat = self._container.get_archive(path)

                # Extract file content from tar archive
                tar_stream = io.BytesIO()
                for chunk in bits:
                    tar_stream.write(chunk)
                tar_stream.seek(0)

                # Open tar archive and extract file
                with tarfile.open(fileobj=tar_stream, mode="r") as tar:
                    # Get the first (and should be only) member
                    members = tar.getmembers()
                    if members:
                        member = members[0]
                        # Skip symlinks and members with path traversal
                        if member.issym() or member.islnk():
                            responses.append(
                                FileDownloadResponse(
                                    path=path,
                                    content=b"",
                                    error=f"Symlink not allowed: {path}",
                                )
                            )
                            continue
                        if ".." in member.name:
                            responses.append(
                                FileDownloadResponse(
                                    path=path,
                                    content=b"",
                                    error=f"Path traversal in archive member: {member.name}",
                                )
                            )
                            continue
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            content = file_obj.read()
                            responses.append(
                                FileDownloadResponse(
                                    path=path,
                                    content=content,
                                    error=None,
                                )
                            )
                        else:
                            responses.append(
                                FileDownloadResponse(
                                    path=path,
                                    content=b"",
                                    error=f"Could not extract file: {path}",
                                )
                            )
                    else:
                        responses.append(
                            FileDownloadResponse(
                                path=path,
                                content=b"",
                                error=f"File not found in archive: {path}",
                            )
                        )

            except Exception as e:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=b"",
                        error=f"Error downloading file: {e}",
                    )
                )

        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files to the Docker container.

        Uses Docker's put_archive() API to upload files.

        Args:
            files: List of (path, content) tuples to upload

        Returns:
            List of FileUploadResponse objects, one per input file
        """
        responses = []

        for path, content in files:
            try:
                path = _validate_sandbox_path(path)  # noqa: PLW2901

                # Create tar archive in memory
                tar_stream = io.BytesIO()
                with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                    # Create file info
                    tarinfo = tarfile.TarInfo(name=path.split("/")[-1])
                    tarinfo.size = len(content)

                    # Add file to archive
                    tar.addfile(tarinfo, io.BytesIO(content))

                tar_stream.seek(0)

                # Determine target directory
                directory = "/".join(path.split("/")[:-1]) or "/"

                # Upload archive to container
                self._container.put_archive(
                    path=directory,
                    data=tar_stream.read(),
                )

                responses.append(FileUploadResponse(path=path, error=None))

            except Exception as e:
                responses.append(
                    FileUploadResponse(
                        path=path,
                        error=f"Error uploading file: {e}",
                    )
                )

        return responses
