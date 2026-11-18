import type { Card, Prefs } from "./types";

/** The cards to draw, in the order chosen in Settings, without the hidden ones. */
export function visibleCards(cards: Card[], chosen: Prefs["cards"]): Card[] {
  const shown = cards.filter((card) => !chosen.hidden.includes(card.plugin));
  const rank = (card: Card): number => {
    const index = chosen.order.indexOf(card.plugin);
    return index === -1 ? chosen.order.length + cards.indexOf(card) : index;
  };
  return [...shown].sort((a, b) => rank(a) - rank(b));
}

/** Move one card up or down in the saved order. Returns the new order of plugin ids. */
export function moveCard(cards: Card[], order: string[], plugin: string, delta: -1 | 1): string[] {
  const full = visibleCards(cards, { order, hidden: [] }).map((card) => card.plugin);
  const index = full.indexOf(plugin);
  const target = index + delta;
  if (index === -1 || target < 0 || target >= full.length) return full;
  [full[index], full[target]] = [full[target], full[index]];
  return full;
}
