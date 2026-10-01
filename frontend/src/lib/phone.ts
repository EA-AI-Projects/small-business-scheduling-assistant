/** Phone entry helpers. The backend accepts only E.164, so clean pasted values before sending. */

export const E164_PATTERN = /^\+[1-9][0-9]{1,14}$/;

/**
 * Remove what copying from Contacts or Messages adds: invisible format characters (directional
 * marks), whitespace including NBSP, and the separators - . ( ). Keeps one leading plus.
 * Does not otherwise reformat the number.
 */
export function normalizePhone(value: string): string {
  return value.replace(/[\p{Cf}\s\-.()]/gu, "");
}

export function isE164(value: string): boolean {
  return E164_PATTERN.test(value);
}
