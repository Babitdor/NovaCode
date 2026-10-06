"""PDF text extraction utility for converting PDF content blocks to text.

When the read_file tool encounters a PDF, it returns a ToolMessage with a
content block of type "file" containing base64-encoded PDF data. Most LLM
backends (especially Ollama) don't support "file" type content blocks, so
we intercept these and extract the text content instead.

Only PDFs are converted. Other multimodal blocks — ``.ppt``/``.pptx`` file
blocks, and ``image``/``audio``/``video`` blocks — are left for the model and
the provider to handle: deepagents emits them deliberately, and the providers
that accept them read them natively. See
:func:`convert_file_content_block_to_text`.
"""

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maximum characters per extracted page (prevents runaway extraction on
# PDFs with very dense text like research papers)
_MAX_CHARS_PER_PAGE = 50_000

# Maximum total characters across all pages
_MAX_TOTAL_CHARS = 500_000


def extract_text_from_base64_pdf(b64_data: str) -> str | None:
    """Extract text from a base64-encoded PDF.

    Args:
        b64_data: Base64-encoded PDF content.

    Returns:
        Extracted text content, or None if extraction failed.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed — cannot extract PDF text")
        return None

    try:
        pdf_bytes = base64.b64decode(b64_data)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.error(f"Failed to open PDF: {e}")
        return None

    try:
        pages_text: list[str] = []
        total_chars = 0

        for page_num in range(len(doc)):
            page = doc[page_num]
            page_text = page.get_text("text")
            if len(page_text) > _MAX_CHARS_PER_PAGE:
                page_text = page_text[:_MAX_CHARS_PER_PAGE] + "\n... [page text truncated]"
            pages_text.append(f"--- Page {page_num + 1} ---\n{page_text}")
            total_chars += len(page_text)
            if total_chars >= _MAX_TOTAL_CHARS:
                pages_text.append("\n... [remaining pages truncated]")
                break

        full_text = "\n\n".join(pages_text)
        if not full_text.strip():
            return None
        return full_text
    except Exception as e:
        logger.error(f"Failed to extract text from PDF: {e}")
        return None
    finally:
        doc.close()


def convert_file_content_block_to_text(
    content: list[str | dict[str, Any]],
) -> list[str | dict[str, Any]]:
    """Convert PDF 'file' content blocks in a ToolMessage content list to text.

    For each content block with type "file" and mime_type "application/pdf",
    the base64 data is decoded and text is extracted using PyMuPDF. The block
    is replaced with a text block containing the extracted text.

    **Only PDFs are touched.** A ``file`` block for any other MIME type
    (``.ppt``/``.pptx``, or a provider-managed reference with no ``base64``) is
    left exactly as it is: deepagents emits those deliberately, and the
    providers that accept them (OpenAI, Google) read them natively. Replacing
    one with "[Unsupported file type: …]" destroyed a block the model could
    have used, and did so for every provider — including the ones that support
    it. Blocks of type ``image``/``audio``/``video`` are likewise untouched;
    they are not this function's business.

    Args:
        content: The content list from a ToolMessage (list of str/dict).

    Returns:
        Modified content list with PDF file blocks replaced by text blocks, or
        the original list (by identity) when there was no PDF to convert.
    """
    if not isinstance(content, list):
        return content

    new_content: list[str | dict[str, Any]] = []
    converted_pdf = False

    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "file"
            and block.get("mime_type") == "application/pdf"
            and block.get("base64")
        ):
            converted_pdf = True
            text = extract_text_from_base64_pdf(block["base64"])
            if text:
                new_content.append({
                    "type": "text",
                    "text": f"[PDF content extracted as text]\n\n{text}",
                })
            else:
                new_content.append({
                    "type": "text",
                    "text": "[PDF file could not be read — text extraction failed. "
                            "Install PyMuPDF (pip install PyMuPDF) for PDF support.]",
                })
        else:
            new_content.append(block)

    return new_content if converted_pdf else content


def sanitize_messages_file_blocks(messages: list) -> list:
    """Scan a list of messages for ToolMessages carrying PDF content blocks.

    This is a safety net for messages that might contain PDF ``file`` blocks
    (e.g., from restored session history) that weren't converted at the
    tool-call layer. Converts those to text blocks so they don't crash backends
    like Ollama.

    Only PDF blocks are converted — see
    :func:`convert_file_content_block_to_text`. A ``.pptx`` or a provider-managed
    file reference is left alone, so a provider that can read it still does.

    Args:
        messages: List of BaseMessage instances.

    Returns:
        The same list (possibly modified in-place) with PDF blocks converted.
        Returns the original list unchanged if no PDF blocks were found.
    """
    from langchain_core.messages import ToolMessage

    modified = False
    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content
        if not isinstance(content, list):
            continue
        # Only a PDF file block is ours to convert; anything else is left for
        # the model and its provider.
        if not any(
            isinstance(b, dict)
            and b.get("type") == "file"
            and b.get("mime_type") == "application/pdf"
            for b in content
        ):
            continue
        # Convert PDF blocks to text
        converted = convert_file_content_block_to_text(content)
        if converted is not content:
            messages[i] = ToolMessage(
                content=converted,
                tool_call_id=msg.tool_call_id,
                name=msg.name if hasattr(msg, "name") else None,
            )
            modified = True

    return messages