import axios from "axios";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const client = axios.create({ baseURL: API, withCredentials: true });

export function errMsg(e) {
  const d = e?.response?.data?.detail;
  if (d == null) return e?.message || "Something went wrong";
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join(" ");
  return String(d);
}

export const api = {
  get: (p, cfg) => client.get(p, cfg).then((r) => r.data),
  post: (p, b, cfg) => client.post(p, b, cfg).then((r) => r.data),
  patch: (p, b) => client.patch(p, b).then((r) => r.data),
  del: (p) => client.delete(p).then((r) => r.data),
};

export async function downloadCsv(path, filename) {
  const res = await client.get(path, { responseType: "blob" });
  const url = window.URL.createObjectURL(new Blob([res.data]));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

export default client;
