from __future__ import annotations
import hashlib
import os
from dataclasses import dataclass

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma


def _source_fingerprint(path: str) -> str:
    """Short stable fingerprint of (path, size, mtime) to detect source changes."""
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


@dataclass
class PdfRag:
    persist_dir: str = "chroma_db"
    collection_name: str = "business_context"

    def build(self, pdf_path: str) -> Chroma:
        ext = os.path.splitext(pdf_path)[1].lower()
        if ext == ".pdf":
            docs = PyPDFLoader(pdf_path).load()
        elif ext in (".md", ".markdown", ".txt"):
            docs = TextLoader(pdf_path, encoding="utf-8").load()
        else:
            raise ValueError(f"PdfRag.build: unsupported file extension {ext!r} for {pdf_path!r}")

        splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=150)
        chunks = splitter.split_documents(docs)

        # Tag every chunk with a fingerprint of the source so we can drop stale
        # chunks when the same collection is rebuilt against a different file.
        fingerprint = _source_fingerprint(pdf_path)
        for c in chunks:
            c.metadata = {**(c.metadata or {}), "source_fingerprint": fingerprint}

        embeddings = OpenAIEmbeddings()
        vectordb = Chroma(
            collection_name=self.collection_name,
            embedding_function=embeddings,
            persist_directory=self.persist_dir,
        )

        # Idempotent rebuild: purge any prior chunks not matching this source.
        try:
            existing = vectordb.get(include=["metadatas"])
            stale_ids = [
                _id for _id, md in zip(existing.get("ids", []), existing.get("metadatas", []))
                if (md or {}).get("source_fingerprint") != fingerprint
            ]
            if stale_ids:
                vectordb.delete(ids=stale_ids)
                print(f"[PdfRag] Purged {len(stale_ids)} stale chunks from previous source.")
        except Exception as exc:
            print(f"[PdfRag] WARN: could not purge stale chunks: {exc}")

        # Only add chunks if the collection doesn't already contain this source.
        existing_for_source = vectordb.get(where={"source_fingerprint": fingerprint}, include=[])
        if not existing_for_source.get("ids"):
            vectordb.add_documents(chunks)
            print(f"[PdfRag] Indexed {len(chunks)} chunks from {os.path.basename(pdf_path)}.")
        else:
            print(f"[PdfRag] Reusing {len(existing_for_source['ids'])} cached chunks for {os.path.basename(pdf_path)}.")

        return vectordb

    def retriever(self, vectordb: Chroma, k: int = 6):
        return vectordb.as_retriever(search_kwargs={"k": k})
