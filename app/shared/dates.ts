// Reading a due date typed the way people say it. Local time; no Electron or DOM.

const DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

const pad = (n: number): string => String(n).padStart(2, "0");

export function isoDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function build(year: number, month: number, day: number): Date | null {
  const date = new Date(year, month, day);
  return date.getFullYear() === year && date.getMonth() === month && date.getDate() === day ? date : null;
}

function plus(now: Date, days: number): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + days);
}

/** A day and month without a year means the next time that date comes round. */
function yearless(now: Date, month: number, day: number): Date | null {
  const today = plus(now, 0);
  const this_year = build(today.getFullYear(), month, day);
  if (this_year && this_year >= today) return this_year;
  return build(today.getFullYear() + 1, month, day);
}

/**
 * The date a person meant, as YYYY-MM-DD, "" for no date, or null when it is not understood.
 * Takes: today, tomorrow, a weekday (the next one), +3 / +3d / +2w, 2026-12-01, 1/12 or
 * 1/12/2026 (day first), 20 Nov, Nov 20.
 */
export function parseDue(text: string, now: Date = new Date()): string | null {
  const word = text.trim().toLowerCase().replace(/\s+/g, " ");
  if (!word) return "";
  if (word === "today") return isoDate(plus(now, 0));
  if (word === "tomorrow" || word === "tmrw" || word === "tom") return isoDate(plus(now, 1));

  const day = word.length >= 3 ? DAYS.findIndex((name) => name.startsWith(word)) : -1; // wed, thurs, friday
  if (day >= 0) {
    const ahead = ((day - now.getDay() + 6) % 7) + 1; // never today: that is "today"
    return isoDate(plus(now, ahead));
  }

  const later = /^(?:\+|in )(\d{1,3}) ?(d|day|days|w|week|weeks)?$/.exec(word);
  if (later) return isoDate(plus(now, Number(later[1]) * (later[2]?.startsWith("w") ? 7 : 1)));

  const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(word);
  if (iso) {
    const date = build(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));
    return date ? isoDate(date) : null;
  }

  const slashed = /^(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?$/.exec(word);
  if (slashed) {
    const [d, m] = [Number(slashed[1]), Number(slashed[2]) - 1];
    if (slashed[3]) {
      const year = slashed[3].length === 2 ? 2000 + Number(slashed[3]) : Number(slashed[3]);
      const date = build(year, m, d);
      return date ? isoDate(date) : null;
    }
    const date = yearless(now, m, d);
    return date ? isoDate(date) : null;
  }

  const named = /^(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3,9})(?: (\d{4}))?$|^([a-z]{3,9}) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?$/.exec(word);
  if (named) {
    const dayNumber = Number(named[1] ?? named[5]);
    const monthName = (named[2] ?? named[4]).slice(0, 3);
    const year = named[3] ?? named[6];
    const month = MONTHS.indexOf(monthName);
    if (month < 0) return null;
    const date = year ? build(Number(year), month, dayNumber) : yearless(now, month, dayNumber);
    return date ? isoDate(date) : null;
  }
  return null;
}

/** "Fri 20 Nov" for a YYYY-MM-DD date, to show what a typed date was understood as. */
export function describeDue(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}
