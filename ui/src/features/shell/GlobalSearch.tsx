// Global contract search (submits to /contracts?q=), in the navigation panel.
import { useNavigate } from "@tanstack/react-router";
import { Search } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";
import styles from "./Sidebar.module.css";

export function GlobalSearch({ onSearch }: { onSearch?: () => void }) {
  const navigate = useNavigate();
  const id = useId();
  const [query, setQuery] = useState("");

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const q = query.trim();
    void navigate({ to: "/contracts", search: q ? { q } : {} });
    onSearch?.();
  }

  return (
    <form role="search" className={styles.search} onSubmit={submit}>
      <label htmlFor={id} className={styles.searchLabel}>
        Search contracts
      </label>
      <div className={styles.searchShell} data-control="single">
        <Search
          size={16}
          strokeWidth={2}
          aria-hidden="true"
          className={styles.searchIcon}
        />
        <input
          id={id}
          type="search"
          className={styles.searchInput}
          placeholder="Search contracts"
          autoComplete="off"
          value={query}
          maxLength={200}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
        />
      </div>
    </form>
  );
}
