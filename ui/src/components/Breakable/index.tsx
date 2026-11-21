// Breakable (layout audit): a dotted, slashed or colon-separated identifier
// ("contract.version_published", "acme/payments/ledger", "urn:acme:orders") that may wrap only
// after a separator, never inside a word, so a narrow table cell never splits it per character.
// The parts stay sibling text nodes around <wbr>, so the element's text is the whole value.
import { Fragment } from "react";

export function Breakable({ text }: { text: string }) {
  const parts = text.split(/(?<=[._:/])/);
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={`${String(index)}-${part}`}>
          {index > 0 ? <wbr /> : null}
          {part}
        </Fragment>
      ))}
    </>
  );
}
