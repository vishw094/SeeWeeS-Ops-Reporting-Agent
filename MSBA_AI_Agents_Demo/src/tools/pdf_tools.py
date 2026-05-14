from __future__ import annotations
from dataclasses import dataclass

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma


@dataclass
class PdfRag:
    persist_dir: str = "chroma_db"
    collection_name: str = "business_context"

    def build(self, pdf_path: str) -> Chroma:
        docs = PyPDFLoader(pdf_path).load()
        splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=150)
        chunks = splitter.split_documents(docs)

        embeddings = OpenAIEmbeddings()
        vectordb = Chroma(
            collection_name=self.collection_name,
            embedding_function=embeddings,
            persist_directory=self.persist_dir,
        )

        # Workshop-friendly: always add chunks (wipe chroma_db if you want clean runs)
        vectordb.add_documents(chunks)
        vectordb.persist()
        return vectordb

    def retriever(self, vectordb: Chroma, k: int = 6):
        return vectordb.as_retriever(search_kwargs={"k": k})
