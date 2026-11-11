"""Error handling and recovery strategies for deepagents CLI."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from novacode_cli.errors.taxonomy import ErrorCategory, RecoverableError


@dataclass
class RecoveryResult:
    """Result of error recovery attempt.

    Attributes:
        success: Whether recovery was successful
        message: Human-readable message about the recovery attempt
        suggestion: Optional suggestion for user action
        new_state: Optional new state to merge into context
    """

    success: bool
    message: str
    suggestion: str | None = None
    new_state: dict | None = None


class ErrorRecoveryStrategy(Protocol):
    """Protocol for error recovery strategies."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this strategy can handle the error.

        Args:
            error: The recoverable error to check

        Returns:
            True if this strategy can handle the error
        """
        ...

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Attempt to recover from the error.

        Args:
            error: The recoverable error to recover from

        Returns:
            Result of the recovery attempt
        """
        ...


class FileNotFoundRecovery:
    """Recover from file not found errors by searching."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a file not found error."""
        return error.category == ErrorCategory.FILE_NOT_FOUND

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest searching for the file using glob.

        Args:
            error: The file not found error

        Returns:
            Recovery result with search suggestion
        """
        file_name = error.context.get("file_name", "")
        if not file_name:
            return RecoveryResult(
                success=False,
                message=f"File not found: {file_name}",
                suggestion="Please check the file path and try again.",
            )

        # Extract just the filename without path
        base_name = Path(file_name).name

        # Suggest searching for similar files
        suggestion = (
            f"I couldn't find '{file_name}'. "
            f"Let me search for similar files: `glob('**/*{base_name}')`\n"
            "If you know the correct path, please provide it."
        )

        return RecoveryResult(
            success=False,
            message=f"File not found: {file_name}",
            suggestion=suggestion,
            new_state={"search_pattern": f"**/*{base_name}"},
        )


class ContextOverflowRecovery:
    """Recover from context overflow by suggesting summarization."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a context overflow error."""
        return error.category == ErrorCategory.CONTEXT_OVERFLOW

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest summarization and memory storage.

        Args:
            error: The context overflow error

        Returns:
            Recovery result with context management suggestions
        """
        return RecoveryResult(
            success=False,
            message="Context limit approaching or exceeded.",
            suggestion=(
                "The conversation context is getting too large. I can:\n"
                "1. Summarize our progress and save to memory\n"
                "2. Focus on a specific area of the codebase\n"
                "3. Use pagination (read_file with limit) instead of full reads\n\n"
                "What would you like me to do?"
            ),
        )


class NetworkErrorRecovery:
    """Recover from network errors with exponential backoff retry."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a network error."""
        return error.category == ErrorCategory.NETWORK_ERROR

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Implement exponential backoff retry.

        Args:
            error: The network error

        Returns:
            Recovery result indicating retry status
        """
        retry_count = error.context.get("retry_count", 0)
        max_retries = 3

        if retry_count >= max_retries:
            return RecoveryResult(
                success=False,
                message=f"Network error after {max_retries} retries: {error.original_error}",
                suggestion="Please check your internet connection and try again.",
            )

        # Exponential backoff: 1s, 2s, 4s
        wait_time = 2**retry_count
        await asyncio.sleep(wait_time)

        return RecoveryResult(
            success=True,  # Indicate retry is possible
            message=f"Network error, retrying in {wait_time}s... (attempt {retry_count + 1}/{max_retries})",
            new_state={"retry_count": retry_count + 1},
        )


class PermissionDeniedRecovery:
    """Recover from permission denied errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a permission denied error."""
        return error.category == ErrorCategory.PERMISSION_DENIED

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest checking and fixing permissions.

        Args:
            error: The permission denied error

        Returns:
            Recovery result with permission fix suggestions
        """
        file_path = error.context.get("file_path", "")

        suggestion = (
            f"Permission denied for: {file_path}\n\n"
            "To fix this, you may need to:\n"
            "1. Check file permissions: `ls -la {file_path}`\n"
            "2. Make file executable: `chmod +x {file_path}`\n"
            "3. Change ownership if needed: `sudo chown $USER {file_path}`"
        )

        return RecoveryResult(
            success=False,
            message=f"Permission denied: {file_path}",
            suggestion=suggestion,
        )


class CommandNotFoundRecovery:
    """Recover from command not found errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a command not found error."""
        return error.category == ErrorCategory.COMMAND_NOT_FOUND

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest installing the missing command.

        Args:
            error: The command not found error

        Returns:
            Recovery result with installation suggestions
        """
        command = error.context.get("command", "")

        suggestion = (
            f"Command not found: {command}\n\n"
            "To fix this, you may need to:\n"
            "1. Check if package is installed: `which {command}`\n"
            "2. Install the package (examples):\n"
            "   - Python: `uv add {command}`\n"
            "   - Node: `npm install -g {command}`\n"
            "   - System: `sudo apt install {command}` or `brew install {command}`"
        )

        return RecoveryResult(
            success=False,
            message=f"Command not found: {command}",
            suggestion=suggestion,
        )


class ImportErrorRecovery:
    """Recover from import errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is an import error."""
        return error.category == ErrorCategory.IMPORT_ERROR

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest installing missing packages or fixing imports.

        Args:
            error: The import error

        Returns:
            Recovery result with installation suggestions
        """
        module = error.context.get("module", "")

        suggestion = (
            f"Module not found: {module}\n\n"
            "Possible fixes:\n"
            f"1. Install the package: `uv add {module}`\n"
            "2. Check if you're in the correct virtual environment\n"
            "3. Verify the import statement spelling\n"
            "4. Check if the module is in the same directory"
        )

        return RecoveryResult(
            success=False,
            message=f"Import error: {module}",
            suggestion=suggestion,
        )


class TypeErrorRecovery:
    """Recover from type errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a type error."""
        return error.category == ErrorCategory.TYPE_ERROR

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest fixing type mismatches.

        Args:
            error: The type error

        Returns:
            Recovery result with type fix suggestions
        """
        expected_type = error.context.get("expected_type", "")
        actual_type = error.context.get("actual_type", "")
        function_name = error.context.get("function", "")

        suggestion = (
            f"Type mismatch: expected {expected_type}, got {actual_type}\n\n"
            "To fix:\n"
            "1. Check function signature for expected argument types\n"
            "2. Convert the value to the correct type\n"
            "3. Use type hints to understand expected types\n"
            "4. Run type checker: `check_types(path)`"
        )

        return RecoveryResult(
            success=False,
            message=f"Type error in {function_name}" if function_name else "Type error",
            suggestion=suggestion,
        )


class TestFailureRecovery:
    """Recover from test failures."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a test failure."""
        return error.category == ErrorCategory.TEST_FAILURE

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Analyze test failure and suggest fixes.

        Args:
            error: The test failure

        Returns:
            Recovery result with test fix suggestions
        """
        test_name = error.context.get("test_name", "")
        assertion = error.context.get("assertion", "")

        suggestion = (
            f"Test failed: {test_name}\n\n"
            f"Assertion: {assertion}\n\n"
            "To fix:\n"
            "1. Read the test file to understand expected behavior\n"
            "2. Check the assertion message for details\n"
            "3. Compare expected vs actual values\n"
            "4. Fix the underlying code, not the test (unless test is wrong)"
        )

        return RecoveryResult(
            success=False,
            message=f"Test failure: {test_name}",
            suggestion=suggestion,
        )


class DependencyErrorRecovery:
    """Recover from dependency errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a dependency error."""
        return error.category == ErrorCategory.DEPENDENCY_ERROR

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest resolving dependency issues.

        Args:
            error: The dependency error

        Returns:
            Recovery result with dependency resolution suggestions
        """
        package = error.context.get("package", "")
        version_conflict = error.context.get("version_conflict", False)

        if version_conflict:
            suggestion = (
                f"Version conflict for {package}\n\n"
                "To fix:\n"
                f"1. Check current versions: `uv pip list | grep {package}`\n"
                f"2. Update package: `uv add {package} --upgrade`\n"
                "3. Reinstall dependencies: `uv sync --reinstall`\n"
                "4. Use virtual environment to isolate dependencies"
            )
        else:
            suggestion = (
                f"Missing dependency: {package}\n\n"
                "To fix:\n"
                f"1. Install missing dependency: `uv add {package}`\n"
                "2. Or sync all dependencies: `uv sync`"
            )

        return RecoveryResult(
            success=False,
            message=f"Dependency error: {package}",
            suggestion=suggestion,
        )


class ConfigurationErrorRecovery:
    """Recover from configuration errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a configuration error."""
        return error.category == ErrorCategory.CONFIGURATION_ERROR

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest fixing configuration issues.

        Args:
            error: The configuration error

        Returns:
            Recovery result with configuration fix suggestions
        """
        config_file = error.context.get("config_file", "")
        env_var = error.context.get("env_var", "")

        if env_var:
            suggestion = (
                f"Missing environment variable: {env_var}\n\n"
                "To fix:\n"
                f"1. Set the environment variable: `export {env_var}=value`\n"
                "2. Or add to .env file: `echo '{env_var}=value' >> .env`\n"
                "3. Or run: `Nova secrets set {env_var.lower()}`"
            )
        else:
            suggestion = (
                f"Configuration error in {config_file}\n\n"
                "To fix:\n"
                "1. Check configuration file syntax\n"
                "2. Verify required fields are present\n"
                "3. Check file permissions\n"
                "4. Look for example configuration files"
            )

        return RecoveryResult(
            success=False,
            message=f"Configuration error: {env_var or config_file}",
            suggestion=suggestion,
        )


class TimeoutErrorRecovery:
    """Recover from timeout errors."""

    def can_handle(self, error: RecoverableError) -> bool:
        """Check if this is a timeout error."""
        return error.category == ErrorCategory.TIMEOUT_ERROR

    async def recover(self, error: RecoverableError) -> RecoveryResult:
        """Suggest handling timeouts.

        Args:
            error: The timeout error

        Returns:
            Recovery result with timeout handling suggestions
        """
        operation = error.context.get("operation", "operation")

        suggestion = (
            f"Operation timed out: {operation}\n\n"
            "To fix:\n"
            "1. Increase timeout if the operation legitimately needs more time\n"
            "2. Break the operation into smaller chunks\n"
            "3. Use pagination for large data operations\n"
            "4. Check for infinite loops or performance issues\n"
            "5. Consider async/parallel processing"
        )

        return RecoveryResult(
            success=False,
            message=f"Timeout: {operation}",
            suggestion=suggestion,
        )


class ErrorHandler:
    """Central error handler with recovery strategies.

    This class provides error classification and recovery for common
    error scenarios in the deepagents CLI.
    """

    def __init__(self):
        """Initialize error handler with all recovery strategies."""
        self.strategies: list[ErrorRecoveryStrategy] = [
            FileNotFoundRecovery(),
            ContextOverflowRecovery(),
            NetworkErrorRecovery(),
            PermissionDeniedRecovery(),
            CommandNotFoundRecovery(),
            ImportErrorRecovery(),
            TypeErrorRecovery(),
            TestFailureRecovery(),
            DependencyErrorRecovery(),
            ConfigurationErrorRecovery(),
            TimeoutErrorRecovery(),
        ]

    def classify_error(self, error: Exception, context: dict | None = None) -> RecoverableError:
        """Classify an error into a category.

        Args:
            error: The exception to classify
            context: Optional additional context about the error

        Returns:
            RecoverableError with classification and recovery info
        """
        context = context or {}

        error_str = str(error).lower()

        # File errors
        if "no such file" in error_str or "file not found" in error_str:
            return RecoverableError(
                category=ErrorCategory.FILE_NOT_FOUND,
                original_error=error,
                context=context,
                recovery_suggestion="Search for the file using glob or check the path",
                user_message=f"File not found: {context.get('file_name', 'unknown')}",
            )

        # Permission errors
        if "permission denied" in error_str or "access denied" in error_str:
            return RecoverableError(
                category=ErrorCategory.PERMISSION_DENIED,
                original_error=error,
                context=context,
                recovery_suggestion="Check file permissions with `ls -la`",
                user_message="Permission denied. You may need to change file permissions.",
            )

        # Command not found errors
        if "command not found" in error_str or "not recognized" in error_str:
            return RecoverableError(
                category=ErrorCategory.COMMAND_NOT_FOUND,
                original_error=error,
                context=context,
                recovery_suggestion="Check if the command is installed",
                user_message=f"Command not found: {context.get('command', 'unknown')}",
            )

        # Import errors
        if "import" in error_str and ("not found" in error_str or "no module" in error_str):
            return RecoverableError(
                category=ErrorCategory.IMPORT_ERROR,
                original_error=error,
                context=context,
                recovery_suggestion="Install missing package or fix import statement",
                user_message=f"Import error: {context.get('module', 'unknown module')}",
            )

        # Type errors
        if "type" in error_str and ("error" in error_str or "mismatch" in error_str):
            return RecoverableError(
                category=ErrorCategory.TYPE_ERROR,
                original_error=error,
                context=context,
                recovery_suggestion="Check function signature and argument types",
                user_message=f"Type error: {error}",
            )

        # Test failures
        if "assertion" in error_str or "test failed" in error_str:
            return RecoverableError(
                category=ErrorCategory.TEST_FAILURE,
                original_error=error,
                context=context,
                recovery_suggestion="Read test output and fix underlying code",
                user_message=f"Test failure: {context.get('test_name', 'unknown test')}",
            )

        # Network errors
        if any(x in error_str for x in ["timeout", "connection", "network", "unreachable"]):
            # Distinguish timeout from other network errors
            if "timeout" in error_str:
                return RecoverableError(
                    category=ErrorCategory.TIMEOUT_ERROR,
                    original_error=error,
                    context=context,
                    recovery_suggestion="Increase timeout or optimize operation",
                    user_message=f"Operation timed out: {context.get('operation', 'unknown')}",
                )
            return RecoverableError(
                category=ErrorCategory.NETWORK_ERROR,
                original_error=error,
                context=context,
                recovery_suggestion="Retry with exponential backoff",
                user_message="Network error occurred. Retrying...",
            )

        # Context overflow
        if "context" in error_str and ("limit" in error_str or "too large" in error_str):
            return RecoverableError(
                category=ErrorCategory.CONTEXT_OVERFLOW,
                original_error=error,
                context=context,
                recovery_suggestion="Summarize and use pagination",
                user_message="Context limit reached. Need to summarize or narrow scope.",
            )

        # Syntax errors
        if "syntax" in error_str or "invalid syntax" in error_str:
            return RecoverableError(
                category=ErrorCategory.SYNTAX_ERROR,
                original_error=error,
                context=context,
                recovery_suggestion="Check code syntax and fix issues",
                user_message=f"Syntax error: {error}",
            )

        # Dependency errors
        if "dependency" in error_str or ("version" in error_str and "conflict" in error_str):
            return RecoverableError(
                category=ErrorCategory.DEPENDENCY_ERROR,
                original_error=error,
                context=context,
                recovery_suggestion="Resolve dependency version conflicts",
                user_message=f"Dependency error: {context.get('package', 'unknown package')}",
            )

        # Configuration errors
        if "config" in error_str or ("environment" in error_str and "variable" in error_str):
            return RecoverableError(
                category=ErrorCategory.CONFIGURATION_ERROR,
                original_error=error,
                context=context,
                recovery_suggestion="Check configuration and environment variables",
                user_message=f"Configuration error: {context.get('config_file', context.get('env_var', 'unknown'))}",
            )

        # Generic tool error
        return RecoverableError(
            category=ErrorCategory.TOOL_ERROR,
            original_error=error,
            context=context,
            recovery_suggestion="Check the error message and try a different approach",
            user_message=f"Tool error: {error}",
        )

    async def handle(self, error: Exception, context: dict | None = None) -> RecoveryResult:
        """Handle error with appropriate recovery strategy.

        Args:
            error: The exception to handle
            context: Optional additional context about the error

        Returns:
            Result of the recovery attempt
        """
        classified = self.classify_error(error, context)

        # Try recovery strategies
        for strategy in self.strategies:
            if strategy.can_handle(classified):
                result = await strategy.recover(classified)
                if result.success or not classified.retry_allowed:
                    return result

        # No recovery strategy worked - surface to user
        return RecoveryResult(
            success=False,
            message=classified.user_message,
            suggestion=classified.recovery_suggestion,
        )
