import { useEffect, useMemo, useState } from "react";

import {
  applyFreecadEdit,
  getFreecadStatus,
  listFreecadDocuments,
  listFreecadObjects,
  previewFreecadEdit,
  API_BASE_URL,
  uploadBluecadFreecadFile,
  listBluecadFreecadReferences,
  addBluecadFreecadReference,
  removeBluecadFreecadReference,
  type BluecadFreecadReference,
  type BluecadFreecadUpload,
  type FreecadEditPreview,
  type FreecadObject,
  type FreecadStatus
} from "../../api/client";

type Props = Readonly<{
  workspaceId: string;
  candidateId: string | null;
  freecadRefs: BluecadFreecadReference[];
  onFreecadRefsChange: (references: BluecadFreecadReference[]) => void;
}>;
type Property = { name: string; value: unknown; readOnly: boolean };

function propertiesOf(object: FreecadObject): Property[] {
  const raw = object.Properties ?? object.properties ?? {};
  return Object.entries(raw).map(([name, entry]) => {
    if (entry && typeof entry === "object" && !Array.isArray(entry) && ("value" in entry || "Value" in entry)) {
      const record = entry as Record<string, unknown>;
      return { name, value: record.value ?? record.Value, readOnly: Boolean(record.read_only ?? record.readonly ?? record.ReadOnly) };
    }
    return { name, value: entry, readOnly: false };
  });
}

function display(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value) ?? "";
}

function parseValue(raw: string, old: unknown): unknown {
  if (typeof old === "boolean") return raw === "true";
  if (typeof old === "number") return Number(raw);
  return raw;
}

export default function FreecadPanel({ workspaceId, candidateId, freecadRefs, onFreecadRefsChange }: Props) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<FreecadStatus | null>(null);
  const [documents, setDocuments] = useState<string[]>([]);
  const [document, setDocument] = useState("");
  const [objects, setObjects] = useState<FreecadObject[]>([]);
  const [objectName, setObjectName] = useState("");
  const [propertyName, setPropertyName] = useState("");
  const [value, setValue] = useState("");
  const [preview, setPreview] = useState<FreecadEditPreview | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [freecadUpload, setFreecadUpload] = useState<BluecadFreecadUpload | null>(null);
  const [freecadObjectName, setFreecadObjectName] = useState("");

  const selectedObject = objects.find((item) => item.Name === objectName);
  const properties = useMemo(() => selectedObject ? propertiesOf(selectedObject) : [], [selectedObject]);
  const selectedProperty = properties.find((item) => item.name === propertyName);

  useEffect(() => {
    if (!selectedProperty) return;
    setValue(display(selectedProperty.value));
    setPreview(null);
  }, [propertyName, selectedProperty]);

  useEffect(() => {
    if (!open || !workspaceId) return;
    setMessage(null);
    setStatus(null);
    void getFreecadStatus(workspaceId).then((next) => {
      setStatus(next);
      if (next.connected) return listFreecadDocuments(workspaceId);
      return [];
    }).then((next) => {
      setDocuments(next);
      setDocument(next[0] ?? "");
    }).catch((error: Error) => setMessage(error.message));
  }, [open, workspaceId]);

  useEffect(() => {
    if (!document) { setObjects([]); return; }
    void listFreecadObjects(workspaceId, document).then((next) => {
      setObjects(next);
      setObjectName(next[0]?.Name ?? "");
    }).catch((error: Error) => setMessage(error.message));
  }, [document, workspaceId]);

  useEffect(() => {
    setPropertyName(properties.find((item) => !item.readOnly)?.name ?? "");
  }, [properties]);

  const editPayload = () => ({
    document,
    object: objectName,
    property: propertyName,
    value: parseValue(value, selectedProperty?.value)
  });
  const onPreview = () => {
    setBusy(true);
    void previewFreecadEdit(workspaceId, editPayload()).then((next) => {
      setPreview(next);
      setMessage(null);
    }).catch((error: Error) => setMessage(error.message)).finally(() => setBusy(false));
  };
  const onApply = () => {
    if (!preview || !window.confirm("Confermi l'applicazione della modifica in FreeCAD?")) return;
    setBusy(true);
    void applyFreecadEdit(workspaceId, { ...editPayload(), old_value: preview.old_value }).then((next) => {
      setMessage(next.document_unchanged ? "Il documento non è stato modificato." : `Valore aggiornato: ${display(next.new_value)}`);
      setPreview(null);
      return listFreecadObjects(workspaceId, document).then(setObjects);
    }).catch((error: Error) => setMessage(error.message)).finally(() => setBusy(false));
  };

  const onChooseFile = (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    void uploadBluecadFreecadFile(workspaceId, file).then((next) => {
      setFreecadUpload(next);
      setFreecadObjectName(next.objects[0]?.name ?? "");
      setMessage(null);
    }).catch((error: Error) => setMessage(error.message)).finally(() => setBusy(false));
  };

  const onAddFreecad = () => {
    if (!candidateId || !freecadUpload || !freecadObjectName) return;
    setBusy(true);
    void addBluecadFreecadReference(workspaceId, candidateId, {
      file_sha256: freecadUpload.file_sha256,
      object_name: freecadObjectName
    }).then(() => listBluecadFreecadReferences(workspaceId, candidateId))
      .then(onFreecadRefsChange)
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const onRemoveFreecad = (reference: BluecadFreecadReference) => {
    setBusy(true);
    void removeBluecadFreecadReference(reference).then(() => {
      if (candidateId) return listBluecadFreecadReferences(workspaceId, candidateId);
      return [];
    }).then(onFreecadRefsChange)
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  return <section className="bluecad-freecad" aria-labelledby="freecad-panel-title">
    <button type="button" className="secondary-button" onClick={() => setOpen((current) => !current)} aria-expanded={open}>FreeCAD</button>
    {open && <div className="bluecad-freecad__panel">
      <h2 id="freecad-panel-title">FreeCAD</h2>
      {status && !status.configured && <p role="status">FreeCAD non configurato. Configura il percorso dell&apos;eseguibile per collegarti.</p>}
      {status && status.configured && !status.connected && <p role="status">Add-on FreeCAD non in esecuzione. Avvia il server FreeCAD e riprova.</p>}
      {status?.connected && <p role="status">FreeCAD connesso.</p>}
      {message && <p className="error-banner" role="alert">{message}</p>}
      {status?.connected && <label>Documento<select value={document} onChange={(event) => setDocument(event.target.value)}>{documents.map((item) => <option key={item}>{item}</option>)}</select></label>}
      {status?.connected && objects.length > 0 && <div>
        <h3>Oggetti</h3>
        <ul>{objects.map((item) => <li key={item.Name}><strong>{item.Label ?? item.Name}</strong> · {item.TypeId ?? "FreeCAD"}</li>)}</ul>
        {selectedObject && <details>
          <summary>Proprietà di {selectedObject.Label ?? selectedObject.Name}</summary>
          <dl>{properties.map((property) => <div key={property.name}><dt>{property.name}</dt><dd>{display(property.value)}</dd></div>)}</dl>
        </details>}
        <form onSubmit={(event) => { event.preventDefault(); onPreview(); }}>
          <label>Oggetto<select value={objectName} onChange={(event) => setObjectName(event.target.value)}>{objects.map((item) => <option key={item.Name} value={item.Name}>{item.Label ?? item.Name}</option>)}</select></label>
          <label>Proprietà<select value={propertyName} onChange={(event) => setPropertyName(event.target.value)}>{properties.map((item) => <option key={item.name} value={item.name} disabled={item.readOnly}>{item.name}</option>)}</select></label>
          <label>Nuovo valore<input value={value} onChange={(event) => setValue(event.target.value)} /></label>
          <button type="submit" disabled={busy || !selectedProperty || selectedProperty.readOnly}>Anteprima</button>
        </form>
        {preview && <div role="status"><p>Anteprima: {display(preview.old_value)} → {display(preview.new_value)}</p><button type="button" onClick={onApply} disabled={busy}>Applica</button></div>}
      </div>}
      <h3>Parte FreeCAD nella vista</h3>
      {status && !status.configured && <p role="status">FreeCAD non configurato. Configura il percorso dell&apos;eseguibile per collegarti.</p>}
      {status?.configured && candidateId && <div>
        <label>Scegli file .FCStd<input type="file" accept=".FCStd" onChange={(event) => onChooseFile(event.target.files?.[0])} /></label>
        {freecadUpload && <div>
          <label>Oggetto FreeCAD<select value={freecadObjectName} onChange={(event) => setFreecadObjectName(event.target.value)}>
            {freecadUpload.objects.map((item) => <option key={item.name} value={item.name}>{item.label || item.name}</option>)}
          </select></label>
          <button type="button" disabled={busy || !freecadObjectName} onClick={onAddFreecad}>Aggiungi alla vista</button>
        </div>}
        {freecadRefs.length === 0 && <p>Nessuna parte FreeCAD nella vista.</p>}
        {freecadRefs.map((reference) => <article key={reference.id} className="bluecad-freecad__reference">
          <strong>{reference.document_file_name}</strong>
          <p>{reference.object_label || reference.object_name}</p>
          <p>{reference.fcstd_sha256.slice(0, 12)} · STEP {reference.step_sha256.slice(0, 12)} · GLB {reference.glb_sha256.slice(0, 12)}</p>
          <p>{reference.valid ? "Valida" : `Non valida: ${reference.finding ?? "artefatto modificato"}`}</p>
          {reference.valid && <p>
            {reference.step_url && <a href={`${API_BASE_URL}${reference.step_url}`}>STEP</a>}
            {reference.stl_url && <> · <a href={`${API_BASE_URL}${reference.stl_url}`}>STL</a></>}
            {reference.three_mf_url && <> · <a href={`${API_BASE_URL}${reference.three_mf_url}`}>3MF</a></>}
          </p>}
          <button type="button" disabled={busy} onClick={() => onRemoveFreecad(reference)}>Rimuovi</button>
        </article>)}
      </div>}
    </div>}
  </section>;
}
