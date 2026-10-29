import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Car, Trash2, Download, Repeat } from "lucide-react";
import { useTranslation } from "react-i18next";
const PURPOSES = ["Client visit", "Job site", "Supply run", "Bank / post office", "Meeting", "Other"];
export default function Mileage() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [trips, setTrips] = useState([]);
  const [vehicles, setVehicles] = useState([]);
  const [summary, setSummary] = useState(null);
  const [year, setYear] = useState("2026");
  const [tripOpen, setTripOpen] = useState(false);
  const [vehOpen, setVehOpen] = useState(false);
  const [trip, setTrip] = useState({
    vehicle_id: "",
    trip_date: "",
    miles: "",
    odometer_start: "",
    odometer_end: "",
    origin: "",
    destination: "",
    purpose: "Client visit",
    notes: ""
  });
  const [veh, setVeh] = useState({
    name: "",
    make: "",
    model: "",
    year: "",
    odometer: ""
  });
  const reload = () => {
    api.get(`/mileage/trips?start=${year}-01-01&end=${year}-12-31`).then(setTrips).catch(() => {});
    api.get("/mileage/vehicles").then(setVehicles).catch(() => {});
    api.get(`/mileage/summary?year=${year}`).then(setSummary).catch(() => {});
  };
  useEffect(() => {
    reload();
    setLoaded(true);
  }, [year]);
  const saveTrip = async () => {
    const body = {
      ...trip,
      miles: trip.miles ? parseFloat(trip.miles) : null,
      odometer_start: trip.odometer_start ? parseFloat(trip.odometer_start) : null,
      odometer_end: trip.odometer_end ? parseFloat(trip.odometer_end) : null,
      vehicle_id: trip.vehicle_id || null
    };
    try {
      await api.post("/mileage/trips", body);
      toast.success(t("mileage.tripLogged"));
      setTripOpen(false);
      setTrip({
        vehicle_id: "",
        trip_date: "",
        miles: "",
        odometer_start: "",
        odometer_end: "",
        origin: "",
        destination: "",
        purpose: "Client visit",
        notes: ""
      });
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const saveVehicle = async () => {
    try {
      await api.post("/mileage/vehicles", {
        ...veh,
        year: veh.year ? parseInt(veh.year) : null,
        odometer: veh.odometer ? parseFloat(veh.odometer) : null
      });
      toast.success(t("mileage.vehicleAdded"));
      setVehOpen(false);
      setVeh({
        name: "",
        make: "",
        model: "",
        year: "",
        odometer: ""
      });
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const delTrip = async id => {
    if (!confirm("Delete this trip?")) return;
    try {
      await api.del(`/mileage/trips/${id}`);
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const dupTrip = t => {
    setTrip({
      vehicle_id: t.vehicle_id || "",
      trip_date: "",
      miles: String(t.miles),
      odometer_start: "",
      odometer_end: "",
      origin: t.origin || "",
      destination: t.destination || "",
      purpose: t.purpose || "Client visit",
      notes: t.notes || ""
    });
    setTripOpen(true);
  };
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("mileage.mileageLog")} subtitle="IRS-ready trip tracking — the right standard rate is applied automatically by date." actions={<>
          <Select value={year} onValueChange={setYear}><SelectTrigger className="w-28 bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
            <SelectContent>{["2024", "2025", "2026", "2027"].map(y => <SelectItem key={y} value={y}>{y}</SelectItem>)}</SelectContent></Select>
          <Button variant="outline" onClick={() => downloadCsv(`/mileage/export.csv?year=${year}`, `mileage-log-${year}.csv`)}><Download size={14} className="me-1" />{t("mileage.irsLogCSV")}</Button>
          {canManage && <><Button variant="outline" onClick={() => setVehOpen(true)}><Car size={14} className="me-1" />{t("mileage.vehicle")}</Button>
          <Button onClick={() => setTripOpen(true)} data-testid="trip-add"><Plus size={14} className="me-1" />{t("mileage.logTrip")}</Button></>}
        </>} />
      {summary && <div className="grid grid-cols-3 gap-4">
          <Card className="p-4"><div className="text-xs text-gray-500">{t("mileage.businessMiles")}{year})</div><div className="text-2xl font-semibold">{summary.total_miles.toLocaleString()}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">{t("mileage.deductibleAmount")}</div><div className="text-2xl font-semibold text-emerald-400">{money(summary.total_deductible * 100)}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">{t("mileage.tripsLogged")}</div><div className="text-2xl font-semibold">{summary.trip_count}</div></Card>
        </div>}
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead><tr className="text-start text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 font-medium">{t("mileage.date")}</th><th className="px-4 py-3 font-medium">{t("mileage.route")}</th>
            <th className="px-4 py-3 font-medium">{t("mileage.purpose")}</th><th className="px-4 py-3 font-medium text-end">{t("mileage.miles")}</th>
            <th className="px-4 py-3 font-medium text-end">{t("mileage.deductible")}</th><th className="px-4 py-3"></th>
          </tr></thead>
          <tbody>
            {trips.map(t => <tr key={t.id} className="border-b border-[#1a2030] hover:bg-[#141926]">
                <td className="px-4 py-3 text-xs text-gray-400">{fmtDate(t.trip_date)}</td>
                <td className="px-4 py-3">{t.origin ? `${t.origin} → ${t.destination}` : <span className="text-gray-600">—</span>}</td>
                <td className="px-4 py-3 text-gray-400 text-xs">{t.purpose}</td>
                <td className="px-4 py-3 text-end font-mono">{t.miles}</td>
                <td className="px-4 py-3 text-end font-mono text-emerald-400">{money(t.deductible * 100)}</td>
                <td className="px-4 py-3 text-end whitespace-nowrap">
                  {canManage && <><Button size="sm" variant="ghost" title={t("mileage.logAgain")} onClick={() => dupTrip(t)}><Repeat size={13} /></Button>
                  <Button size="sm" variant="ghost" onClick={() => delTrip(t.id)}><Trash2 size={13} /></Button></>}
                </td>
              </tr>)}
            {!trips.length && <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-500 text-sm">{t("mileage.noTripsIn")} {year} {t("mileage.logYourFirstDriveAbove")}</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={tripOpen} onOpenChange={setTripOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader><DialogTitle>{t("mileage.logATrip")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("mileage.vehicle")}</Label>
              <Select value={trip.vehicle_id || "none"} onValueChange={v => setTrip({
              ...trip,
              vehicle_id: v === "none" ? "" : v
            })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("mileage.select")} /></SelectTrigger>
                <SelectContent><SelectItem value="none">—</SelectItem>{vehicles.map(v => <SelectItem key={v.id} value={v.id}>{v.name}</SelectItem>)}</SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("mileage.date")}</Label><Input type="date" value={trip.trip_date} onChange={e => setTrip({
              ...trip,
              trip_date: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("mileage.miles")}</Label><Input type="number" step="0.1" placeholder={t("mileage.orUseOdometer")} value={trip.miles} onChange={e => setTrip({
              ...trip,
              miles: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("mileage.purpose")}</Label>
              <Select value={trip.purpose} onValueChange={v => setTrip({
              ...trip,
              purpose: v
            })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent>{PURPOSES.map(p => <SelectItem key={p} value={p}>{p}</SelectItem>)}</SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("mileage.odometerStart")}</Label><Input type="number" step="0.1" value={trip.odometer_start} onChange={e => setTrip({
              ...trip,
              odometer_start: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("mileage.odometerEnd")}</Label><Input type="number" step="0.1" value={trip.odometer_end} onChange={e => setTrip({
              ...trip,
              odometer_end: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("mileage.from")}</Label><Input value={trip.origin} onChange={e => setTrip({
              ...trip,
              origin: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("mileage.to")}</Label><Input value={trip.destination} onChange={e => setTrip({
              ...trip,
              destination: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="col-span-2"><Label className="text-xs">{t("mileage.notes")}</Label><Input value={trip.notes} onChange={e => setTrip({
              ...trip,
              notes: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button variant="ghost" onClick={() => setTripOpen(false)}>{t("mileage.cancel")}</Button><Button onClick={saveTrip} data-testid="trip-save">{t("mileage.saveTrip")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={vehOpen} onOpenChange={setVehOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>{t("mileage.addVehicle")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2"><Label className="text-xs">{t("mileage.name")}</Label><Input placeholder={t("mileage.workTruck")} value={veh.name} onChange={e => setVeh({
              ...veh,
              name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("mileage.make")}</Label><Input value={veh.make} onChange={e => setVeh({
              ...veh,
              make: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("mileage.model")}</Label><Input value={veh.model} onChange={e => setVeh({
              ...veh,
              model: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("mileage.year")}</Label><Input type="number" value={veh.year} onChange={e => setVeh({
              ...veh,
              year: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("mileage.odometer")}</Label><Input type="number" value={veh.odometer} onChange={e => setVeh({
              ...veh,
              odometer: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button variant="ghost" onClick={() => setVehOpen(false)}>{t("mileage.cancel")}</Button><Button onClick={saveVehicle}>{t("mileage.add")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
