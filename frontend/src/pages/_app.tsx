import type { AppProps } from "next/app";
import Head from "next/head";

import "@/styles/globals.css";

export default function OwnerApp({ Component, pageProps }: AppProps) {
  return (
    <>
      <Head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Scheduling · Owner</title>
      </Head>
      <Component {...pageProps} />
    </>
  );
}
