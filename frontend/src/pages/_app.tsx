import type { AppProps } from "next/app";
import Head from "next/head";

import "@/styles/globals.css";

export default function OwnerApp({ Component, pageProps }: AppProps) {
  return (
    <>
      <Head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Smart Scheduling Assistant · Owner</title>
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
      </Head>
      <Component {...pageProps} />
    </>
  );
}
