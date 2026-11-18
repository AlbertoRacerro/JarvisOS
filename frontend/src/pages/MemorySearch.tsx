import { useCallback, useEffect, useMemo, useState } from "react";
import { listWorkspaces, type Workspace } from "../api/client";
import {
  getRegisteredFolder,
  refreshFolder,
  registerFolder,
  removeFolder,
  searchMemory,
  type DocumentFolderInfo,
  type FolderRefreshResult,
  type MemorySearchItem
} from "../api/memorySearch";

export const MEMORY_SEARCH_TITLE = "Ricerca";
export const MEMORY_SEARCH_EMPTY = "Nessun risultato trovato.";
export const FOLDER_BOX_TITLE = "Cartella documenti";
export const FOLDER_UNREGISTERED = "Nessuna cartella registrata.";
export const FOLDER_INVALID_MESSAGE = "Cartella non valida: serve una cartella esistente, non un collegamento, fuori dai dati di Jarvis e dal codice.";

export const OWNER_NAMES: Record<string, string> = {
  project_knowledge: "Base di progetto",
  literature: "Letteratura",
  modeling: "Modelli",
  process_stack: "Processo",
  engineering: "Ingegneria",
  bluecad: "CAD",
  ai_threads: "Conversazioni",
  development: "Sviluppo",
  workspace_documents: "Documenti"
};

export const ownerLabel = (owner: string): string => OWNER_NAMES[owner] ?? owner;

type Props = Readonly<{
  workspaceId: string | null;
  onWorkspaceChange?: (workspaceId: string) => void;
}>;

export default function MemorySearch({ workspaceId, onWorkspaceChange }: Props) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [results, setResults] = useState<MemorySearchItem[]>([]);

  const [folderInfo, setFolderInfo] = useState<DocumentFolderInfo | null>(null);
  const [folderLoading, setFolderLoading] = useState(false);
  const [folderActionInProgress, setFolderActionInProgress] = useState(false);
  const [folderPathInput, setFolderPathInput] = useState("");
  const [folderError, setFolderError] = useState<string | null>(null);
  const [refreshReport, setRefreshReport] = useState<FolderRefreshResult | null>(null);

  useEffect(() => {
    let alive = true;
    void listWorkspaces()
      .then((items) => {
        if (!alive) return;
        setWorkspaces(items);
        if (!workspaceId && items[0]) {
          onWorkspaceChange?.(items[0].id);
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [onWorkspaceChange, workspaceId]);

  const activeWorkspaceId = useMemo(
    () => workspaceId ?? workspaces[0]?.id ?? null,
    [workspaceId, workspaces]
  );

  const loadFolder = useCallback(() => {
    if (!activeWorkspaceId) {
      setFolderInfo(null);
      return;
    }
    setFolderLoading(true);
    setFolderError(null);
    getRegisteredFolder(activeWorkspaceId)
      .then((info) => {
        setFolderInfo(info);
      })
      .catch(() => {
        setFolderInfo(null);
      })
      .finally(() => {
        setFolderLoading(false);
      });
  }, [activeWorkspaceId]);

  useEffect(() => {
    setResults([]);
    setSearched(false);
    setSearchError(null);
    setRefreshReport(null);
    setFolderError(null);
    loadFolder();
  }, [activeWorkspaceId, loadFolder]);

  const handleSearch = () => {
    const q = searchQuery.trim();
    if (!activeWorkspaceId || !q) return;
    setSearching(true);
    setSearched(true);
    setSearchError(null);
    searchMemory(activeWorkspaceId, q)
      .then((items) => {
        setResults(items);
      })
      .catch((err: Error) => {
        setResults([]);
        setSearchError(err.message || "Ricerca non riuscita.");
      })
      .finally(() => {
        setSearching(false);
      });
  };

  const handleRegisterFolder = () => {
    const path = folderPathInput.trim();
    if (!activeWorkspaceId || !path) return;
    setFolderActionInProgress(true);
    setFolderError(null);
    setRefreshReport(null);
    registerFolder(activeWorkspaceId, path)
      .then((info) => {
        setFolderInfo(info);
        setFolderPathInput("");
      })
      .catch(() => {
        setFolderError(FOLDER_INVALID_MESSAGE);
      })
      .finally(() => {
        setFolderActionInProgress(false);
      });
  };

  const handleRefreshFolder = () => {
    if (!activeWorkspaceId) return;
    setFolderActionInProgress(true);
    setFolderError(null);
    refreshFolder(activeWorkspaceId)
      .then((report) => {
        setRefreshReport(report);
      })
      .catch((err: Error) => {
        setFolderError(err.message || "Aggiornamento cartella non riuscito.");
      })
      .finally(() => {
        setFolderActionInProgress(false);
      });
  };

  const handleRemoveFolder = () => {
    if (!activeWorkspaceId) return;
    setFolderActionInProgress(true);
    setFolderError(null);
    setRefreshReport(null);
    removeFolder(activeWorkspaceId)
      .then((info) => {
        setFolderInfo(info);
      })
      .catch((err: Error) => {
        setFolderError(err.message || "Rimozione cartella non riuscita.");
      })
      .finally(() => {
        setFolderActionInProgress(false);
      });
  };

  return (
    <section className="memory-search" aria-label={MEMORY_SEARCH_TITLE}>
      <header className="memory-search__header">
        <h1>{MEMORY_SEARCH_TITLE}</h1>
      </header>

      {/* Cartella documenti box */}
      <section className="memory-search__folder-box" aria-labelledby="folder-box-title">
        <h2 id="folder-box-title">{FOLDER_BOX_TITLE}</h2>
        {folderLoading ? (
          <p>Caricamento cartella…</p>
        ) : folderInfo?.registered ? (
          <div className="memory-search__folder-registered">
            <p>
              Cartella registrata: <strong>{folderInfo.folder_name}</strong>
            </p>
            <div className="memory-search__folder-actions">
              <button
                type="button"
                onClick={handleRefreshFolder}
                disabled={folderActionInProgress}
              >
                Aggiorna cartella
              </button>
              <button
                type="button"
                onClick={handleRemoveFolder}
                disabled={folderActionInProgress}
              >
                Rimuovi cartella
              </button>
            </div>
            {refreshReport && (
              <div className="memory-search__refresh-report">
                <p>
                  Inclusi: {refreshReport.included}, Saltati: {refreshReport.skipped}, Modificati: {refreshReport.changed}, Eliminati: {refreshReport.deleted}
                </p>
                {refreshReport.reasons.length > 0 && (
                  <ul className="memory-search__refresh-reasons">
                    {refreshReport.reasons.map((r, i) => (
                      <li key={i}>
                        {r.path}: {r.reason}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        ) : (
          <div className="memory-search__folder-unregistered">
            <p>{FOLDER_UNREGISTERED}</p>
            <div className="memory-search__register-form">
              <label htmlFor="folder-path-input">Percorso della cartella</label>
              <input
                id="folder-path-input"
                type="text"
                value={folderPathInput}
                onChange={(e) => setFolderPathInput(e.target.value)}
                placeholder="/percorso/assoluto/cartella"
              />
              <button
                type="button"
                onClick={handleRegisterFolder}
                disabled={folderActionInProgress || !folderPathInput.trim()}
              >
                Registra cartella
              </button>
            </div>
          </div>
        )}
        {folderError && (
          <p className="memory-search__folder-error" role="alert">
            {folderError}
          </p>
        )}
      </section>

      {/* Search bar */}
      <div className="memory-search__bar">
        <label htmlFor="memory-search-input">Cerca nella memoria</label>
        <input
          id="memory-search-input"
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSearch();
          }}
          placeholder="Cerca nella memoria del workspace…"
        />
        <button
          type="button"
          onClick={handleSearch}
          disabled={searching || !searchQuery.trim()}
        >
          {searching ? "Ricerca in corso…" : "Cerca"}
        </button>
      </div>

      {searchError && (
        <p className="memory-search__error" role="alert">
          {searchError}
        </p>
      )}

      {/* Results or Empty State */}
      <div className="memory-search__results">
        {results.length === 0 ? (
          <p className="memory-search__empty">{MEMORY_SEARCH_EMPTY}</p>
        ) : (
          <ul className="memory-search__list">
            {results.map((item, index) => {
              const isDoc = (item.owner || item.authority_owner) === "workspace_documents";
              return (
                <li key={index} className="memory-search__item">
                  <article className="memory-search__card">
                    <header className="memory-search__item-header">
                      <span className="memory-search__owner">
                        {ownerLabel(item.owner || item.authority_owner)}
                      </span>
                      <strong className="memory-search__title">{item.title}</strong>
                      {isDoc ? (
                        <span className="memory-search__doc-locator">
                          {item.relative_path && (
                            <span className="memory-search__relative-path">{item.relative_path}</span>
                          )}
                          {item.heading && (
                            <span className="memory-search__heading"> — {item.heading}</span>
                          )}
                        </span>
                      ) : (
                        item.location && (
                          <span className="memory-search__location">{item.location}</span>
                        )
                      )}
                    </header>
                    <p className="memory-search__excerpt">{item.excerpt}</p>
                  </article>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
