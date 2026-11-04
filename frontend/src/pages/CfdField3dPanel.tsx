import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { getComponentCfdRunFields3d, type CfdField3d, type CfdRun } from "../api/designProject";
import { colorFor, legendGradient } from "./cfdFieldSlice";
import { reflectedSample, transforms, validField3d } from "./cfdField3d";

type Props = { projectId: string; versionId: string; componentId: string; run: CfdRun };
type Field = "p" | "Umag";
type Probe = ReturnType<typeof reflectedSample> & { surface: string; units: string; field: Field; sourceIndex: number };
const fmt = (value: number) => value.toPrecision(4);

/** Solver surfaces in the case frame. A reflected copy is a view of the computed source, never a solver result. */
export default function CfdField3dPanel({ projectId, versionId, componentId, run }: Props) {
  const [payload, setPayload] = useState<CfdField3d | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [field, setField] = useState<Field>("p");
  const [full, setFull] = useState(false);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [ready, setReady] = useState(false);
  const mount = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let active = true;
    setPayload(null); setError(null); setFull(false); setProbe(null);
    getComponentCfdRunFields3d(projectId, versionId, componentId, run.run_id)
      .then((result) => { if (active) setPayload(result); })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "3D fields could not be loaded."); });
    return () => { active = false; };
  }, [projectId, versionId, componentId, run.run_id]);
  const available = payload && validField3d(payload) ? payload : null;
  const range = useMemo(() => {
    if (!available) return null;
    const values = available.surfaces.map((surface) => field === "p"
      ? { min: surface.fields.p.min, max: surface.fields.p.max, units: surface.fields.p.units }
      : { min: surface.fields.U.magnitude_min, max: surface.fields.U.magnitude_max, units: surface.fields.U.units });
    return { min: Math.min(...values.map((value) => value.min)), max: Math.max(...values.map((value) => value.max)), units: values[0]!.units };
  }, [available, field]);
  useEffect(() => {
    const host = mount.current;
    if (!host || !available || !range) return;
    setReady(false);
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true }); }
    catch { setError("Unable to start the 3D viewer in this browser."); return; }
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(getComputedStyle(host).getPropertyValue("--color-bg-technical-viewport").trim() || "#172027");
    const camera = new THREE.PerspectiveCamera(45, 1, 0.001, 1e6);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const meshes: THREE.Mesh[] = [];
    const geometries: THREE.BufferGeometry[] = [];
    const material = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.92 });
    const bounds = new THREE.Box3();
    for (const transform of transforms(available, full)) for (const surface of available.surfaces) {
      const positions: number[] = [], colors: number[] = [], sourceIndices: number[] = [];
      const reverse = transform.position[0] * transform.position[1] * transform.position[2] < 0;
      for (const polygon of surface.polygons) for (let j = 1; j + 1 < polygon.length; j++) {
        const triangle = reverse ? [polygon[0]!, polygon[j + 1]!, polygon[j]!] : [polygon[0]!, polygon[j]!, polygon[j + 1]!];
        for (const index of triangle) {
          const sample = reflectedSample(surface, index, transform);
          positions.push(...sample.position);
          const value = field === "p" ? sample.p : Math.hypot(...sample.U);
          const color = new THREE.Color(colorFor(value, range.min, range.max));
          colors.push(color.r, color.g, color.b);
          sourceIndices.push(index);
          bounds.expandByPoint(new THREE.Vector3(...sample.position));
        }
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
      geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, material);
      mesh.userData = { surface, transform, sourceIndices };
      scene.add(mesh); meshes.push(mesh); geometries.push(geometry);
    }
    const size = bounds.getSize(new THREE.Vector3());
    const center = bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(size.length(), 1e-6);
    camera.position.copy(center).add(new THREE.Vector3(radius * 0.8, radius * 0.6, radius * 0.8));
    camera.near = radius / 10000; camera.far = radius * 100; camera.updateProjectionMatrix();
    controls.target.copy(center); controls.update();
    renderer.domElement.setAttribute("role", "img");
    renderer.domElement.setAttribute("aria-label", "3D CFD computed surfaces; drag to orbit, scroll to zoom, click to probe a vertex");
    host.appendChild(renderer.domElement);
    const resize = () => { const width = Math.max(host.clientWidth, 1), height = Math.max(host.clientHeight, 1);
      camera.aspect = width / height; camera.updateProjectionMatrix(); renderer.setSize(width, height); };
    const observer = new ResizeObserver(resize); observer.observe(host); resize();
    let down: { x: number; y: number } | null = null;
    const onDown = (event: PointerEvent) => { down = { x: event.clientX, y: event.clientY }; };
    const onClick = (event: PointerEvent) => {
      if (!down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 4) return;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects(meshes, false)[0];
      if (!hit?.face) return;
      const mesh = hit.object as THREE.Mesh;
      const { surface, transform, sourceIndices } = mesh.userData as { surface: typeof available.surfaces[number]; transform: ReturnType<typeof transforms>[number]; sourceIndices: number[] };
      const vertices = [hit.face.a, hit.face.b, hit.face.c];
      const attribute = mesh.geometry.getAttribute("position");
      const nearest = vertices.reduce((best, vertex) => hit.point.distanceToSquared(new THREE.Vector3().fromBufferAttribute(attribute, vertex))
        < hit.point.distanceToSquared(new THREE.Vector3().fromBufferAttribute(attribute, best)) ? vertex : best);
      const index = sourceIndices[nearest]!;
      setProbe({ ...reflectedSample(surface, index, transform), surface: surface.name, field,
        units: field === "p" ? surface.fields.p.units : surface.fields.U.units, sourceIndex: index });
    };
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onClick);
    let frame = 0;
    const draw = () => { frame = requestAnimationFrame(draw); controls.update(); renderer.render(scene, camera); };
    draw(); setReady(true);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); controls.dispose(); renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointerup", onClick); renderer.dispose(); material.dispose(); geometries.forEach((geometry) => geometry.dispose()); renderer.domElement.remove(); };
  }, [available, field, full, range]);
  return <section aria-label="CFD 3D field" className="design-project__field-3d">
    <div className="design-project__field-head"><h4>3D field</h4>{available ? <>
      <div role="group" aria-label="3D field" className="design-project__segmented">
        <button type="button" aria-pressed={field === "p"} onClick={() => { setField("p"); setProbe(null); }}>p</button>
        <button type="button" aria-pressed={field === "Umag"} onClick={() => { setField("Umag"); setProbe(null); }}>|U|</button>
      </div>
      {available.symmetry.mirror_planes.length > 0 ? <label><input type="checkbox" checked={full} onChange={(event) => { setFull(event.target.checked); setProbe(null); }} /> Show full pipe</label> : null}
    </> : null}</div>
    {error ? <p role="alert">{error}</p> : payload?.status === "unavailable" || payload?.status === "too_large" ? <p role="status">{payload.reason}</p>
      : payload && !available ? <p role="alert">Malformed 3D field payload.</p> : !payload ? <p role="status">Loading 3D fields…</p> : null}
    {available && range ? <figure className="design-project__field-3d-figure">
      {full ? <strong className="design-project__field-3d-badge">Mirrored from the computed quarter, not computed</strong> : null}
      <div ref={mount} className="design-project__field-3d-canvas" />
      <figcaption><div className="design-project__field-legend" aria-label="3D colour legend">
        <span>{field === "p" ? "p" : "|U|"}</span><span>{fmt(range.min)}</span>
        <span className="design-project__field-legend-bar" style={{ background: legendGradient() }} />
        <span>{fmt(range.max)}</span><span>{range.units}</span>
      </div>
      <p role="status" className="design-project__field-readout">{probe ? <>
        {probe.surface}: {field === "p" ? `p = ${fmt(probe.p)} ${probe.units}` : `|U| = ${fmt(Math.hypot(...probe.U))} ${probe.units}`};
        U = ({probe.U.map(fmt).join(", ")}) m/s at ({probe.position.map(fmt).join(", ")}) m.
        {probe.planes.length ? ` Reconstructed via ${probe.planes.join(" then ")}; computed source point ${probe.sourceIndex + 1} at (${probe.source.map(fmt).join(", ")}) m.`
          : ` Computed vertex ${probe.sourceIndex + 1}.`}
      </> : ready ? "Click a surface to probe its nearest vertex." : "Preparing 3D field…"}</p>
      </figcaption></figure> : null}
  </section>;
}
