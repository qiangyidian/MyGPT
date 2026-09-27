# Dependency audit exceptions

The locked backend dependency audit remains strict except for the four current
ChromaDB advisories listed below. `crewai==1.15.22` requires `chromadb~=1.1.0`,
and no fixed ChromaDB release is available as of 2026-09-27. ChromaDB is not the
application's vector store: the application uses Qdrant, does not start a
ChromaDB server, and constructs CrewAI with `memory=False`.

| Audit ID | Advisory | Reason for exception |
| --- | --- | --- |
| `PYSEC-2026-311` | CVE-2026-45829 | Unfixed ChromaDB server code injection; no Chroma server is exposed by this app. |
| `PYSEC-2026-3813` | CVE-2026-45830 | Unfixed ChromaDB tenant authorization issue; no Chroma server is exposed. |
| `PYSEC-2026-3814` | CVE-2026-45833 | Unfixed ChromaDB model code injection; no Chroma server is exposed. |
| `PYSEC-2026-3815` | CVE-2026-45831 | Unfixed ChromaDB RBAC tenant-scope issue; no Chroma server is exposed. |

The workflow suppresses only these exact IDs. Remove each exception when
CrewAI no longer requires the affected ChromaDB range or an upstream fixed
release becomes available. All other locked dependencies are audited with
strict failure behavior.
