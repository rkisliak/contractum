import { toSlug } from "@/lib/text";

// Suggests a slug from a display name (D-UI-09-04): lowercase a-z, 0-9 and single hyphens, no
// leading or trailing hyphen, at most 63 characters. The user can edit it until the folder is
// created; afterwards it is immutable.
export function suggestSlug(name: string): string {
  return toSlug(name.normalize("NFKD").replace(/[\u0300-\u036f]/g, ""), 63);
}

export function isValidSlug(slug: string): boolean {
  return slug.length <= 63 && /^[a-z0-9]+(-[a-z0-9]+)*$/.test(slug);
}
