import { ALL_CONFIGS } from './config/index.js';
import type { Bracket, JurisdictionConfig } from './config/types.js';
import type { FilingStatus } from './types.js';

export interface JurisdictionHealth {
  jurisdiction: string;
  status: 'ok' | 'stale' | 'invalid';
  taxYear: number;
  lastVerified: string;
  /** Whole days between lastVerified and the as-of date; null if unparseable. */
  ageInDays: number | null;
  provisional: boolean;
  issues: string[];
}

export interface ValidateOptions {
  /** Reference date (ISO yyyy-mm-dd or Date). Defaults to today. */
  asOf?: string | Date;
  /** Configs to validate. Defaults to every registered config. */
  configs?: JurisdictionConfig[];
  /** Maximum allowed age of lastVerified, in days. Default 90. */
  maxAgeDays?: number;
}

const DAY_MS = 86_400_000;

function parseDate(value: string | Date): number | null {
  const t = value instanceof Date ? value.getTime() : Date.parse(`${value}T00:00:00Z`);
  return Number.isNaN(t) ? null : t;
}

function checkBrackets(jurisdiction: string, status: FilingStatus, brackets: Bracket[], issues: string[]): void {
  const tag = `${jurisdiction}/${status}`;
  if (brackets.length === 0) {
    issues.push(`${tag}: bracket table is empty`);
    return;
  }
  let prevUpTo = 0;
  let prevRate = 0;
  brackets.forEach((b, i) => {
    const last = i === brackets.length - 1;
    if (!(b.rate > 0 && b.rate < 1)) {
      issues.push(`${tag}: bracket ${i} rate ${b.rate} is outside (0, 1)`);
    }
    if (b.rate < prevRate) {
      issues.push(`${tag}: bracket ${i} rate ${b.rate} is lower than the previous rate (tables must be progressive)`);
    }
    prevRate = b.rate;
    if (last) {
      if (b.upTo !== null) issues.push(`${tag}: top bracket must be open-ended (upTo null)`);
    } else {
      if (b.upTo === null) {
        issues.push(`${tag}: only the top bracket may be open-ended`);
      } else if (!(b.upTo > prevUpTo)) {
        issues.push(`${tag}: bracket ${i} ceiling ${b.upTo} is not strictly above the previous ceiling ${prevUpTo}`);
      } else {
        prevUpTo = b.upTo;
      }
    }
  });
}

/**
 * Jurisdiction data health:
 *  - freshness: flags any config whose lastVerified is older than
 *    maxAgeDays (default 90) relative to the as-of date;
 *  - schema: bracket ceilings strictly ascending with an open-ended top,
 *    rates within (0, 1) and non-decreasing, caps positive, thresholds
 *    ordered, installment percentages summing to 1, deductions ≥ 0,
 *    stub/no-tax records carrying their required explanatory fields.
 */
export function validateJurisdictions(options: ValidateOptions = {}): JurisdictionHealth[] {
  const configs = options.configs ?? ALL_CONFIGS;
  const maxAgeDays = options.maxAgeDays ?? 90;
  const asOfMs = parseDate(options.asOf ?? new Date()) ?? Date.now();

  return configs.map((cfg) => {
    const issues: string[] = [];
    let stale = false;

    if (!cfg.version) issues.push(`${cfg.jurisdiction}: config version is empty`);
    if (!cfg.source) issues.push(`${cfg.jurisdiction}: source note is empty`);
    if (!Number.isInteger(cfg.taxYear) || cfg.taxYear < 2000) {
      issues.push(`${cfg.jurisdiction}: taxYear ${cfg.taxYear} looks wrong`);
    }

    const verifiedMs = parseDate(cfg.lastVerified);
    let ageInDays: number | null = null;
    if (verifiedMs === null) {
      issues.push(`${cfg.jurisdiction}: lastVerified "${cfg.lastVerified}" is not a valid ISO date`);
    } else {
      ageInDays = Math.floor((asOfMs - verifiedMs) / DAY_MS);
      if (ageInDays > maxAgeDays) {
        stale = true;
        issues.push(
          `${cfg.jurisdiction}: rate data last verified ${cfg.lastVerified} is ${ageInDays} days old (limit ${maxAgeDays})`,
        );
      }
    }

    if (cfg.kind === 'federal') {
      if (!(cfg.se.socialSecurityWageBase > 0)) {
        issues.push('US: Social Security wage base must be positive');
      }
      if (!(cfg.se.netEarningsFactor > 0 && cfg.se.netEarningsFactor <= 1)) {
        issues.push('US: SE net-earnings factor must be within (0, 1]');
      }
      for (const s of Object.keys(cfg.se.additionalMedicareThreshold) as FilingStatus[]) {
        if (!(cfg.se.additionalMedicareThreshold[s] > 0)) {
          issues.push(`US: Additional Medicare threshold for ${s} must be positive`);
        }
        if (!(cfg.qbi.threshold[s] > 0) || !(cfg.qbi.phaseInRange[s] > 0)) {
          issues.push(`US: QBI threshold/phase-in range for ${s} must be positive`);
        }
        if (cfg.standardDeduction[s] < 0) {
          issues.push(`US: standard deduction for ${s} is negative`);
        }
        checkBrackets('US', s, cfg.brackets[s], issues);
      }
      const sum = cfg.installmentPcts.reduce((a, b) => a + b, 0);
      if (Math.abs(sum - 1) > 1e-9) {
        issues.push(`US: installment percentages sum to ${sum}, not 1`);
      }
    } else if (cfg.implementation === 'full') {
      if (!cfg.brackets || !cfg.standardDeduction || !cfg.safeHarbor || !cfg.installmentPcts) {
        issues.push(`${cfg.jurisdiction}: marked 'full' but missing tables`);
      } else {
        for (const s of Object.keys(cfg.brackets) as FilingStatus[]) {
          if (cfg.standardDeduction[s] < 0) {
            issues.push(`${cfg.jurisdiction}: standard deduction for ${s} is negative`);
          }
          checkBrackets(cfg.jurisdiction, s, cfg.brackets[s], issues);
        }
        const sum = cfg.installmentPcts.reduce((a, b) => a + b, 0);
        if (Math.abs(sum - 1) > 1e-9) {
          issues.push(`${cfg.jurisdiction}: installment percentages sum to ${sum}, not 1`);
        }
        if (cfg.surtax && (!(cfg.surtax.threshold > 0) || !(cfg.surtax.rate > 0 && cfg.surtax.rate < 1))) {
          issues.push(`${cfg.jurisdiction}: surtax threshold/rate out of range`);
        }
      }
    } else if (cfg.implementation === 'no-tax') {
      if (!cfg.noTaxNote) {
        issues.push(`${cfg.jurisdiction}: no-tax module must carry an explanatory note`);
      }
    } else if (cfg.implementation === 'stub') {
      if (!cfg.stubReason) {
        issues.push(`${cfg.jurisdiction}: stub module must carry a reason`);
      }
      if (cfg.brackets) {
        issues.push(`${cfg.jurisdiction}: stub must not carry a rate table (it would masquerade as implemented)`);
      }
    }

    const status = issues.some((i) => !i.includes('days old')) ? 'invalid' : stale ? 'stale' : 'ok';
    return {
      jurisdiction: cfg.jurisdiction,
      status,
      taxYear: cfg.taxYear,
      lastVerified: cfg.lastVerified,
      ageInDays,
      provisional: cfg.provisional,
      issues,
    };
  });
}
