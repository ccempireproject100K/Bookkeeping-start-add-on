const fs = require("fs");
const path = require("path");

const dir = path.join(__dirname, "..", "locales");
const load = (c) => JSON.parse(fs.readFileSync(path.join(dir, c, "translation.json"), "utf8"));
const flatten = (o, p = "", out = {}) => {
  Object.entries(o).forEach(([k, v]) => {
    const key = p ? `${p}.${k}` : k;
    if (v && typeof v === "object") flatten(v, key, out);
    else out[key] = v;
  });
  return out;
};
const EXPECTED = ["am","ar","az","bg","bho","bn","bs","ca","ceb","cs","da","de","el","en","es","et","fa","ff","fi","fr","gu","ha","he","hi","hr","ht","hu","hy","id","ig","ilo","it","ja","jv","ka","kk","km","kn","ko","ku","ln","lo","lt","lv","mg","mk","ml","mr","ms","my","ne","nl","no","om","pa","pl","prs","ps","pt","qu","rn","ro","ru","rw","si","sk","sl","sm","so","sq","sr","sv","sw","ta","te","th","ti","tl","tr","tw","uk","ur","uz","vi","wo","yi","yo","yue","zh","zh-CN","zu"];
const en = flatten(load("en"));
const tokens = (s) => (typeof s === "string" ? (s.match(/\{\{[^}]+\}\}/g) || []).sort() : []);

test("all 91 locales exist", () => {
  EXPECTED.forEach((c) => expect(fs.existsSync(path.join(dir, c, "translation.json"))).toBe(true));
});

describe.each(EXPECTED.filter((c) => c !== "en"))("locale %s", (code) => {
  const loc = flatten(load(code));
  test("has every key en has", () => {
    expect(Object.keys(en).filter((k) => !(k in loc))).toEqual([]);
  });
  test("keeps interpolation tokens", () => {
    const bad = Object.keys(en).filter((k) => k in loc && JSON.stringify(tokens(en[k])) !== JSON.stringify(tokens(loc[k])));
    expect(bad).toEqual([]);
  });
});
