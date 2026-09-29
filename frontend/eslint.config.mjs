import next from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const config = [
  ...next,
  ...nextTypescript,
  { ignores: ["out/**", ".next/**", "next-env.d.ts", "src/api/schema.d.ts"] },
];

export default config;
