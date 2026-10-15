import { useRef, useState } from "react";
import axios from "axios";
import { errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import { Mic, Square, Loader2, Send } from "lucide-react";
import { useTranslation } from "react-i18next";
const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
export default function Voice() {
  const {
    t
  } = useTranslation();
  const [loaded] = useState(true);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState([]);
  const [text, setText] = useState("");
  const recRef = useRef(null);
  const chunksRef = useRef([]);
  const ask = async form => {
    setBusy(true);
    try {
      const j = (await axios.post(`${API}/voice/ask`, form, {
        withCredentials: true
      })).data;
      setTurns(t => [...t, j]);
      if (j.audio_url) new Audio(`${API}${j.audio_url}`).play().catch(() => {});
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true
      });
      const rec = new MediaRecorder(stream);
      recRef.current = rec;
      chunksRef.current = [];
      rec.ondataavailable = e => e.data.size && chunksRef.current.push(e.data);
      rec.onstop = () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(chunksRef.current, {
          type: "audio/webm"
        });
        const form = new FormData();
        form.append("audio", blob, "question.webm");
        ask(form);
      };
      rec.start();
      setRecording(true);
    } catch {
      toast.error(t("voice.microphoneUnavailableCheckBrowserPermission"));
    }
  };
  const stop = () => {
    recRef.current?.stop();
    setRecording(false);
  };
  const sendText = () => {
    if (!text.trim()) return;
    const form = new FormData();
    form.append("text", text.trim());
    setText("");
    ask(form);
  };
  if (!loaded) return <Loading />;
  return <div className="space-y-6 max-w-2xl mx-auto">
      <PageHeader title={t("voice.voiceBooks")} subtitle="Talk to your books — get a voice-note answer back." />
      <Card className="p-6 flex flex-col items-center gap-4">
        <Button onClick={recording ? stop : start} disabled={busy} className={`rounded-full w-24 h-24 ${recording ? "bg-rose-600 hover:bg-rose-500" : "bg-blue-600 hover:bg-blue-500"}`} data-testid="voice-mic">
          {busy ? <Loader2 size={32} className="animate-spin" /> : recording ? <Square size={28} /> : <Mic size={32} />}
        </Button>
        <p className="text-xs text-gray-500">
          {busy ? "Thinking…" : recording ? "Listening — tap to stop" : "Tap the mic and ask, e.g. “How much am I owed this month?”"}
        </p>
        <div className="flex gap-2 w-full">
          <Input placeholder={t("voice.orTypeYourQuestion")} value={text} onChange={e => setText(e.target.value)} onKeyDown={e => e.key === "Enter" && sendText()} className="bg-[#0F1218] border-[#222838]" />
          <Button onClick={sendText} disabled={busy}><Send size={14} /></Button>
        </div>
      </Card>
      {turns.map((t, i) => <Card key={i} className="p-5 space-y-3">
          <p className="text-sm text-blue-300">“{t.transcript}”</p>
          <p className="text-sm text-gray-200">{t.reply}</p>
          {t.audio_url && <audio controls src={`${API}${t.audio_url}`} className="w-full h-9" />}
        </Card>)}
      {!turns.length && <Card className="p-8 text-center text-gray-500 text-sm">{t("voice.noQuestionsYetYourBooksAre")}</Card>}
    </div>;
}
