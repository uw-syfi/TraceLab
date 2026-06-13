// Cost math + price resolution for the Phase-1 mock. The PRICE TABLE itself is NOT here — it lives in
// the single-source JSON at artifacts/utils/pricing.json, the same file the Python analytics aggregator
// reads, so prices are edited in exactly one place. In the real path Python computes cost and the
// payload carries dollars; this module exists only so the mock can produce matching numbers. The
// resolve rules (exact -> family substring -> unpriced) mirror what Python implements over the JSON.
import pricingTable from '../../../../../artifacts/utils/pricing.json';

export interface ModelPrice {
  inputPerM: number;
  cachedInputPerM: number;
  outputPerM: number;
}

/** When the prices in pricing.json were last reviewed — surface near any cost number as a disclaimer. */
export const PRICING_AS_OF: string = (pricingTable as { as_of: string }).as_of;

const EXACT = pricingTable.exact as Record<string, ModelPrice>;
const FAMILY = pricingTable.family as ({ match: string } & ModelPrice)[];

/** Look up a price for (provider, model). Returns null when truly unknown (caller marks unpriced). */
export function priceFor(provider: string, model: string | null | undefined): ModelPrice | null {
  if (!model) return null;
  const exact = EXACT[`${provider}:${model}`];
  if (exact) return exact;
  const m = model.toLowerCase();
  for (const f of FAMILY) {
    if (m.includes(f.match)) return { inputPerM: f.inputPerM, cachedInputPerM: f.cachedInputPerM, outputPerM: f.outputPerM };
  }
  return null;
}

export interface RoundTokens {
  prefixTokens: number;
  appendTokens: number;
  outputTokens: number;
  reasoningTokens?: number;
}

export interface RoundCost {
  inputCost: number;
  cachedCost: number;
  outputCost: number;
  reasoningCost: number;
  total: number;
}

const PER_TOKEN = (perM: number) => perM / 1_000_000;

/** Cost of one round (or a summed bucket) given its token split and a price. */
export function roundCost(price: ModelPrice, t: RoundTokens): RoundCost {
  const inputCost = t.appendTokens * PER_TOKEN(price.inputPerM);
  const cachedCost = t.prefixTokens * PER_TOKEN(price.cachedInputPerM);
  const outputCost = t.outputTokens * PER_TOKEN(price.outputPerM);
  const reasoningCost = (t.reasoningTokens ?? 0) * PER_TOKEN(price.outputPerM);
  return { inputCost, cachedCost, outputCost, reasoningCost, total: inputCost + cachedCost + outputCost };
}

/** What prefix caching saved vs. billing those cached tokens at the fresh-input rate. */
export function cacheSavings(price: ModelPrice, prefixTokens: number): number {
  return prefixTokens * PER_TOKEN(price.inputPerM - price.cachedInputPerM);
}
