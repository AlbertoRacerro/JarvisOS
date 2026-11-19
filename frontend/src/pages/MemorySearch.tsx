import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listWorkspaces, type Workspace } from "../api/client";
import {
  getRegisteredFolder,
  refreshFolder,
  registerFolder,
  removeFolder,
  runRetrievalEval,
  searchMemory,
  MemorySearchError,
  type DocumentFolderInfo,
  type FolderRefreshResult,
  type MemorySearchItem,
  type RetrievalEvalResult
} from "../api/memorySearch";

export const MEMORY_SEARCH_TITLE = "Ricerca";
export const MEMORY_SEARCH_EMPTY = "Nessun risultato trovato.";
export const MEMORY_SEARCH_HINT = "Scrivi una o più parole e premi Cerca: Jarvis cerca nella memoria di questo workspace.";
export const FOLDER_BOX_TITLE = "Cartella documenti";
export const FOLDER_UNREGISTERED = "Nessuna cartella registrata.";
export const FOLDER_INVALID_MESSAGE = "Cartella non valida: serve una cartella esistente, non un collegamento, fuori dai dati di Jarvis e dal codice e che non li contenga.";

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

export const FOLDER_LOAD_ERROR = "Impossibile leggere lo stato della cartella.";

const VERDICT_LABELS: Record<RetrievalEvalResult["verdict"], string> = {
  usable: "Utilizzabile",
  usable_with_warnings: "Utilizzabile con avvertenze",
  unchecked: "Non verificato",
  not_usable: "Non utilizzabile"
};

const SKIP_REASONS: Record<string, string> = {
  binary: "file binario",
  directory_limit: "troppe sottocartelle",
  file_too_large: "file troppo grande",
  invalid_utf8: "testo non leggibile (codifica)",
  missing_or_symlink_root: "cartella mancante o collegamento",
  passage_limit: "troppi passaggi",
  passage_too_large: "passaggio troppo lungo",
  path_too_long: "percorso troppo lungo",
  symlink_directory: "sottocartella collegamento, ignorata",
  symlink_or_special: "collegamento o file speciale, ignorato",
  unreadable: "file non leggibile",
  unreadable_or_changed: "file non leggibile o cambiato durante la lettura",
  unreadable_root: "cartella non leggibile"
};

export const skipReasonLabel = (reason: string): string => {
  const limit = /^file_limit:(\d+)$/.exec(reason);
  if (limit) return `troppi file: ${limit[1]} esclusi`;
  return SKIP_REASONS[reason] ?? reason;
};

export const skipPathLabel = (path: string): string => (path === "." ? "cartella" : path);

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
  const [evalQuery, setEvalQuery] = useState("");
  const [evalExpected, setEvalExpected] = useState("");
  const [evalMode, setEvalMode] = useState<"lexical" | "hybrid">("lexical");
  const [evalRunning, setEvalRunning] = useState(false);
  const [evalError, setEvalError] = useState<string | null>(null);
  const [evalResult, setEvalResult] = useState<RetrievalEvalResult | null>(null);

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
  // A response for a workspace that is no longer selected is dropped.
  const currentWorkspace = useRef(activeWorkspaceId);
  currentWorkspace.current = activeWorkspaceId;
  const stillCurrent = (requested: string) => currentWorkspace.current === requested;

  const loadFolder = useCallback(() => {
    if (!activeWorkspaceId) {
      setFolderInfo(null);
      return;
    }
    const requested = activeWorkspaceId;
    setFolderLoading(true);
    setFolderError(null);
    getRegisteredFolder(requested)
      .then((info) => {
        if (currentWorkspace.current === requested) setFolderInfo(info);
      })
      .catch(() => {
        if (currentWorkspace.current !== requested) return;
        setFolderInfo(null);
        setFolderError(FOLDER_LOAD_ERROR);
      })
      .finally(() => {
        if (currentWorkspace.current === requested) setFolderLoading(false);
      });
  }, [activeWorkspaceId]);

  useEffect(() => {
    setResults([]);
    setSearching(false);
    setSearched(false);
    setSearchError(null);
    setRefreshReport(null);
    setFolderError(null);
    setEvalResult(null);
    setEvalError(null);
    loadFolder();
  }, [activeWorkspaceId, loadFolder]);

  const handleSearch = () => {
    const q = searchQuery.trim();
    if (!activeWorkspaceId || !q) return;
    const requested = activeWorkspaceId;
    setSearching(true);
    setSearched(true);
    setSearchError(null);
    searchMemory(requested, q)
      .then((items) => {
        if (stillCurrent(requested)) setResults(items);
      })
      .catch(() => {
        if (!stillCurrent(requested)) return;
        setResults([]);
        setSearchError("Ricerca non riuscita.");
      })
      .finally(() => {
        if (stillCurrent(requested)) setSearching(false);
      });
  };

  const handleRetrievalEval = () => {
    const query = evalQuery.trim();
    const expected = evalExpected.trim();
    if (!activeWorkspaceId || !query || !expected) {
      setEvalError("Inserisci una domanda e almeno un titolo o ID atteso.");
      return;
    }
    const requested = activeWorkspaceId;
    setEvalRunning(true);
    setEvalError(null);
    runRetrievalEval(requested, {
      mode: evalMode,
      questions: [{ query, expected_ids: [expected] }]
    })
      .then((result) => {
        if (stillCurrent(requested)) setEvalResult(result);
      })
      .catch(() => {
        if (stillCurrent(requested)) {
          setEvalResult(null);
          setEvalError("Verifica del recupero non riuscita.");
        }
      })
      .finally(() => {
        if (stillCurrent(requested)) setEvalRunning(false);
      });
  };

  const evalMetric = (name: string): number | null => {
    const output = evalResult?.outputs.find((item) => item.name === name);
    return output ? output.value.value : null;
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
      .catch((err: unknown) => {
        const refused = err instanceof MemorySearchError && err.code === "folder_invalid";
        setFolderError(refused ? FOLDER_INVALID_MESSAGE : "Registrazione cartella non riuscita.");
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
      .catch(() => {
        setFolderError("Aggiornamento cartella non riuscito.");
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
      .catch(() => {
        setFolderError("Rimozione cartella non riuscita.");
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
        ) : folderInfo === null && folderError === FOLDER_LOAD_ERROR ? null : folderInfo?.registered ? (
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
                        {skipPathLabel(r.path)}: {skipReasonLabel(r.reason)}
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

      <section className="memory-search__retrieval-eval" aria-labelledby="retrieval-eval-title">
        <h2 id="retrieval-eval-title">Verifica del recupero</h2>
        <label htmlFor="retrieval-eval-query">Domanda</label>
        <input
          id="retrieval-eval-query"
          type="text"
          value={evalQuery}
          onChange={(e) => setEvalQuery(e.target.value)}
        />
        <label htmlFor="retrieval-eval-expected">Titolo o ID atteso</label>
        <input
          id="retrieval-eval-expected"
          type="text"
          value={evalExpected}
          onChange={(e) => setEvalExpected(e.target.value)}
        />
        <label htmlFor="retrieval-eval-mode">Modalità</label>
        <select id="retrieval-eval-mode" value={evalMode} onChange={(e) => setEvalMode(e.target.value as "lexical" | "hybrid")}>
          <option value="lexical">Lessicale</option>
          <option value="hybrid">Ibrida</option>
        </select>
        <button type="button" onClick={handleRetrievalEval} disabled={evalRunning || !activeWorkspaceId}>
          {evalRunning ? "Verifica in corso…" : "Verifica recupero"}
        </button>
        {evalError && <p role="alert">{evalError}</p>}
        {evalResult && (
          <div className="memory-search__retrieval-eval-result">
            <p>Esito della misura: <strong>{VERDICT_LABELS[evalResult.verdict]}</strong></p>
            {evalMetric("top3_hit_rate") === 0 && (
              <p>La risposta attesa non compare tra i primi tre risultati: controlla il titolo o l'ID atteso.</p>
            )}
            <p>Risposta attesa al primo posto (top1_hit_rate): {evalMetric("top1_hit_rate") ?? "—"}</p>
            <p>Risposta attesa tra i primi tre (top3_hit_rate): {evalMetric("top3_hit_rate") ?? "—"}</p>
            <p>Posizione media della risposta attesa (mrr): {evalMetric("mrr") ?? "—"}</p>
            <p>Domande senza alcun risultato (zero_hit_count): {evalMetric("zero_hit_count") ?? "—"}</p>
          </div>
        )}
      </section>

      {/* Results or Empty State */}
      <div className="memory-search__results">
        {results.length === 0 ? (
          !searched ? <p className="memory-search__hint">{MEMORY_SEARCH_HINT}</p>
            : !searching && !searchError && <p className="memory-search__empty">{MEMORY_SEARCH_EMPTY}</p>
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
