import { toCents } from './money.js';
import { computeFederalDetailed } from './federal.js';
import { mergeSchedules } from './schedule.js';
import type { StateConfig } from './config/types.js';
import type { JurisdictionResult, ResultStatus, TaxProfile, TaxResult } from './types.js';
import type { StateCalculator } from './states/engine.js';

import * as california from './states/california.js';
import * as newyork from './states/newyork.js';
import * as tx from './states/tx.js';
import * as fl from './states/fl.js';
import * as wa from './states/wa.js';
import * as nv from './states/nv.js';
import * as sd from './states/sd.js';
import * as wy from './states/wy.js';
import * as ak from './states/ak.js';
import * as tn from './states/tn.js';
import * as nh from './states/nh.js';
import * as pa from './states/pa.js';
import * as il from './states/il.js';
import * as nc from './states/nc.js';
import * as ga from './states/ga.js';
import * as az from './states/az.js';

interface StateModule {
  config: StateConfig;
  calculate: StateCalculator;
}

/**
 * The state registry. Every state module is a separate file exporting the
 * same { config, calculate } interface, so adding or completing a state
 * never changes the core. Jurisdiction keys compose as:
 *   'US'          federal only
 *   'US-CA'       federal + California
 *   'US-NY-NYC'   federal + New York State + NYC locality segment
 * Locality segments are part of the key design from day one; NYC is the
 * first reserved locality and currently reports NOT_IMPLEMENTED.
 */
const STATE_MODULES: Record<string, StateModule> = {
  CA: california,
  NY: newyork,
  TX: tx,
  FL: fl,
  WA: wa,
  NV: nv,
  SD: sd,
  WY: wy,
  AK: ak,
  TN: tn,
  NH: nh,
  PA: pa,
  IL: il,
  NC: nc,
  GA: ga,
  AZ: az,
};

/** Localities the key design reserves: state code → locality codes. */
const KNOWN_LOCALITIES: Record<string, string[]> = {
  NY: ['NYC'],
};

export class UnknownJurisdictionError extends Error {
  constructor(key: string) {
    super(
      `Unknown jurisdiction "${key}". Supported keys: ${listJurisdictions().join(', ')}. ` +
        'States outside the registry have no module yet — an unlisted state is an explicit error, never a silent zero.',
    );
    this.name = 'UnknownJurisdictionError';
  }
}

export function listJurisdictions(): string[] {
  const keys = ['US'];
  for (const code of Object.keys(STATE_MODULES)) {
    keys.push(`US-${code}`);
    for (const loc of KNOWN_LOCALITIES[code] ?? []) keys.push(`US-${code}-${loc}`);
  }
  return keys;
}

export function getStateModule(stateCode: string): StateModule | undefined {
  return STATE_MODULES[stateCode];
}

/** The tax year this build computes. Rate tables are versioned per year in
 *  src/config; adding a year is a data change plus a registry entry. */
export const TAX_YEAR = 2026;

/**
 * Core interface: estimate total tax for a freelancer.
 *
 * @param netProfit  Schedule C net profit in dollars (may be ≤ 0).
 * @param jurisdiction  'US', 'US-XX', or 'US-NY-NYC' style key.
 * @param taxProfile  Filing status, prior-year figures, payments, flags.
 */
export function calculateTax(
  netProfit: number,
  jurisdiction: string,
  taxProfile: TaxProfile,
): TaxResult {
  if (!Number.isFinite(netProfit)) {
    throw new Error(`netProfit must be a finite number, got ${netProfit}`);
  }
  const key = jurisdiction.trim().toUpperCase();
  const parts = key.split('-');
  if (parts[0] !== 'US' || parts.length > 3) throw new UnknownJurisdictionError(jurisdiction);
  const stateCode = parts[1];
  const locality = parts[2];

  const netProfitCents = toCents(netProfit);
  const federal = computeFederalDetailed(netProfitCents, taxProfile);
  const notes: string[] = [];
  const workpapers = [federal.result.workpaper];
  let state: JurisdictionResult | undefined;
  let status: ResultStatus = 'OK';

  if (stateCode !== undefined) {
    const mod = STATE_MODULES[stateCode];
    if (!mod) throw new UnknownJurisdictionError(jurisdiction);
    if (locality !== undefined && !(KNOWN_LOCALITIES[stateCode] ?? []).includes(locality)) {
      throw new UnknownJurisdictionError(jurisdiction);
    }
    state = mod.calculate(
      {
        netProfitCents,
        federalAgiCents: federal.agiCents,
        profile: taxProfile,
      },
      locality,
    );
    workpapers.push(state.workpaper);
    if (state.status === 'NOT_IMPLEMENTED') {
      status = 'PARTIAL';
      notes.push(
        `State tax for ${key} is NOT_IMPLEMENTED and is EXCLUDED from the total below — the total is federal only. A missing module is never reported as a $0 state liability.`,
      );
    }
    if (state.localityStatus === 'NOT_IMPLEMENTED') {
      status = 'PARTIAL';
      notes.push(
        `Locality tax for ${locality} is NOT_IMPLEMENTED and is excluded from the total.`,
      );
    }
  }

  const breakdown = [
    ...federal.result.components,
    ...(state && state.status === 'OK' ? state.components : []),
  ];
  const totalLiabilityCents =
    federal.result.totalTaxCents + (state && state.status === 'OK' ? state.totalTaxCents : 0);

  const scheduleParts = [{ jurisdiction: 'US', schedule: federal.result.schedule }];
  if (state && state.status === 'OK') {
    scheduleParts.push({ jurisdiction: state.jurisdiction, schedule: state.schedule });
  }
  const schedule = mergeSchedules(scheduleParts);
  const dueDates = schedule.map((p) => p.dueDate);

  const requiredAnnualPaymentCents =
    federal.result.safeHarbor.requiredAnnualPaymentCents +
    (state && state.status === 'OK' ? state.safeHarbor.requiredAnnualPaymentCents : 0);

  if (federal.result.workpaper.provisional) {
    notes.push('Federal figures rely on a PROVISIONAL config — see workpapers.');
  }
  if (state?.workpaper.provisional) {
    notes.push(`${state.jurisdiction} figures rely on a PROVISIONAL config — see workpapers.`);
  }
  notes.push(...federal.result.notes);
  if (state) notes.push(...state.notes);

  return {
    jurisdiction: key,
    taxYear: TAX_YEAR,
    status,
    totalLiabilityCents,
    breakdown,
    federal: federal.result,
    ...(state ? { state } : {}),
    safeHarbor: {
      federal: federal.result.safeHarbor,
      ...(state ? { state: state.safeHarbor } : {}),
      requiredAnnualPaymentCents,
    },
    schedule,
    dueDates,
    notes,
    workpapers,
  };
}
