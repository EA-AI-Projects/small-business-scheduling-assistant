import Link from "next/link";

export default function NotFound() {
  return (
    <main className="welcome card">
      <h2>Page not found</h2>
      <p><Link href="/">Return to the owner workspace</Link></p>
    </main>
  );
}
