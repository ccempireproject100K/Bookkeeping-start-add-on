import {
  AppWindow, Bath, BatteryCharging, Briefcase, Bug, Building, Building2,
  Calculator, Car, ClipboardCheck, Cpu, CreditCard, DoorOpen, Droplet,
  Droplets, Fence, Hammer, Home, Key, Laptop, Layers, Leaf, LifeBuoy,
  Paintbrush, Plug, Ruler, Scissors, Smartphone, Snowflake, Sparkles, Store,
  Sun, Trash2, TreeDeciduous, TreePine, Waves, Wrench, Zap,
} from "lucide-react";

/** Per-trade visual identity. The icon name comes from the niche pack
 *  (backend/niches/*.yaml `icon:`); anything unknown falls back to a
 *  neutral Briefcase — never another trade's imagery. */
const ICONS = {
  AppWindow, Bath, BatteryCharging, Bug, Building, Building2, Calculator,
  Car, ClipboardCheck, Cpu, CreditCard, DoorOpen, Droplet, Droplets, Fence,
  Hammer, Home, Key, Laptop, Layers, Leaf, LifeBuoy, Paintbrush, Plug,
  Ruler, Scissors, Smartphone, Snowflake, Sparkles, Store, Sun, Trash2,
  TreeDeciduous, TreePine, Waves, Wrench, Zap,
};

export default function FaceIcon({ name, ...props }) {
  const Cmp = ICONS[name] || Briefcase;
  return <Cmp {...props} />;
}
