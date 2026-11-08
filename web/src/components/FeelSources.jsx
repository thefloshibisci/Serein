import { useEffect, useState } from "react";
import { authoredType } from "../storage/authoredTypes.js";

export const isFeeling = scene => scene?.memory_role === "feel" || scene?.semantic_kind === "feel";

export function FeelSources({ scene }) {
  const [data, setData] = useState(null), [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setData(null); setError("");
    authoredType("read_feel", { feel_id: scene.canonicalSceneId || scene.id }, controller.signal)
      .then(result => { if (!controller.signal.aborted) setData(result); })
      .catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [scene.id, scene.canonicalSceneId, scene.sourceUpdatedAt]);
  const sources = data?.readable ? data.feel_sources || [] : [];
  return <section className="scene-reference-section feel-sources" aria-label="感受来源">
    <h3>当时的感受</h3><p>这是主观感受，不作为事情发生的证据。</p>
    {error ? <p role="alert">{error}</p> : !data ? <p role="status">正在核对来源…</p>
      : !data.readable ? <p>这段感受当前不可读取。</p>
      : sources.length ? <ul>{sources.map((source, index) => <li key={`${source.kind}:${source.id}:${index}`}>
        {source.status === "available" ? <><strong>{source.title || "原话来源"}</strong>
          <span> · {source.kind === "event" ? "事件" : source.kind === "scene" ? "Scene" : "原话"} · 当时版本 {source.revision}</span>
          {source.summary && <p>{source.summary}</p>}</> : <p>来源当前不可读取。</p>}
      </li>)}</ul> : <p>来源信息待补充。</p>}
  </section>;
}
