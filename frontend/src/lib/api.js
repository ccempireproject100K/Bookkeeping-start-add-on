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
};

export default client;
