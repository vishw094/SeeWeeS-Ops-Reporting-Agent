from __future__ import annotations
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma


_FINGERPRINT_FILE = "_source_fingerprint.txt"
_CONTEXT_CACHE_FILE = "_business_context.txt"


def _source_fingerprint(path: str) -> str:
    """Stable fingerprint of (path, size, mtime). Tracks source changes."""
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def _read_fingerprint(persist_dir: str) -> str:
    """Return the fingerprint of the source currently indexed in persist_dir, or '' if none."""
    p = os.path.join(persist_dir, _FINGERPRINT_FILE)
    if not os.path.exists(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_fingerprint(persist_dir: str, fingerprint: str) -> None:
    os.makedirs(persist_dir, exist_ok=True)
    with open(os.path.join(persist_dir, _FINGERPRINT_FILE), "w", encoding="utf-8") as f:
        f.write(fingerprint)


def load_cached_business_context(persist_dir: str) -> str:
    """Return the previously-extracted business_context for the indexed source, or ''."""
    p = os.path.join(persist_dir, _CONTEXT_CACHE_FILE)
    if not os.path.exists(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def save_cached_business_context(persist_dir: str, context: str) -> None:
    """Persist business_context alongside the chroma index so reruns skip the LLM call."""
    os.makedirs(persist_dir, exist_ok=True)
    with open(os.path.join(persist_dir, _CONTEXT_CACHE_FILE), "w", encoding="utf-8") as f:
        f.write(context)


def _split_markdown_by_section(md_path: str) -> list[Document]:
    """Split a markdown file into one Document per heading block, then chunk.

    Each chunk carries `section_title` and `source_name` metadata. Reference
    tables (appendix / DQ rules / weather triggers) get larger chunks so a
    full table survives in a single retrieval hit.
    """
    text = Path(md_path).read_text(encoding="utf-8")
    name = os.path.basename(md_path)

    sections: list[tuple[str, str]] = []
    title = "Document Overview"
    buf: list[str] = []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            if buf:
                sections.append((title, "\n".join(buf).strip()))
            title = re.sub(r"^#{1,6}\s+", "", line).strip()
            buf = []
            continue
        buf.append(line)
    if buf:
        sections.append((title, "\n".join(buf).strip()))

    chunks: list[Document] = []
    for section_title, body in sections:
        if not body:
            continue
        is_reference = "|" in body or any(
            m in section_title.lower()
            for m in ("appendix", "data quality", "weather", "buffer", "reporting", "corridor")
        )
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=1400 if is_reference else 1000,
            chunk_overlap=150,
        )
        doc = Document(
            page_content=f"{section_title}\n\n{body}",
            metadata={"source_name": name, "section_title": section_title,
                      "stream": "reference" if is_reference else "policy"},
        )
        chunks.extend(splitter.split_documents([doc]))
    return chunks


@dataclass
class PdfRag:
    persist_dir: str = "chroma_db"
    collection_name: str = "business_context"

    def build(self, pdf_path: str):
        ext = os.path.splitext(pdf_path)[1].lower()
        if ext == ".pdf":
            docs = PyPDFLoader(pdf_path).load()
            splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=150)
            chunks = splitter.split_documents(docs)
        elif ext in (".md", ".markdown", ".txt"):
            # Section-aware split: each markdown heading becomes its own
            # document tagged with section_title, so retrieval evaluation can
            # measure section recall and the planner sees coherent rule blocks.
            chunks = _split_markdown_by_section(pdf_path)
        else:
            raise ValueError(f"PdfRag.build: unsupported file extension {ext!r} for {pdf_path!r}")

        fingerprint = _source_fingerprint(pdf_path)
        prior_fingerprint = _read_fingerprint(self.persist_dir)

        # Source changed (or first run with a different file) → wipe & rebuild.
        # We do this at the filesystem level because chromadb's rust backend
        # has been known to crash on `.get()` calls under Windows.
        if prior_fingerprint and prior_fingerprint != fingerprint and os.path.isdir(self.persist_dir):
            shutil.rmtree(self.persist_dir, ignore_errors=True)
            print(f"[PdfRag] Source changed (fingerprint {prior_fingerprint} → {fingerprint}); wiped persist_dir.")
            prior_fingerprint = ""

        embeddings = OpenAIEmbeddings()
        vectordb = Chroma(
            collection_name=self.collection_name,
            embedding_function=embeddings,
            persist_directory=self.persist_dir,
        )

        if prior_fingerprint == fingerprint:
            print(f"[PdfRag] Reusing cached index for {os.path.basename(pdf_path)} (fingerprint {fingerprint}).")
        else:
            vectordb.add_documents(chunks)
            _write_fingerprint(self.persist_dir, fingerprint)
            print(f"[PdfRag] Indexed {len(chunks)} chunks from {os.path.basename(pdf_path)} (fingerprint {fingerprint}).")

        return vectordb

    def retriever(self, vectordb: Chroma, k: int = 6):
        return vectordb.as_retriever(search_kwargs={"k": k})

    def retrieve(self, vectordb: Chroma, query: str, k: int = 6, stream: str | None = None):
        """Similarity search with optional policy/reference stream filter."""
        if stream:
            docs = vectordb.similarity_search(query, k=k, filter={"stream": stream})
            if docs:
                return docs
        return vectordb.similarity_search(query, k=k)
