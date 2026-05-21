from __future__ import annotations
import hashlib
import os
import shutil
from dataclasses import dataclass

from langchain_community.document_loaders import PyPDFLoader, TextLoader
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


@dataclass
class PdfRag:
    persist_dir: str = "chroma_db"
    collection_name: str = "business_context"

    def build(self, pdf_path: str):
        ext = os.path.splitext(pdf_path)[1].lower()
        if ext == ".pdf":
            docs = PyPDFLoader(pdf_path).load()
        elif ext in (".md", ".markdown", ".txt"):
            docs = TextLoader(pdf_path, encoding="utf-8").load()
        else:
            raise ValueError(f"PdfRag.build: unsupported file extension {ext!r} for {pdf_path!r}")

        splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=150)
        chunks = splitter.split_documents(docs)

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
