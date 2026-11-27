# ClearQuery for Claude Desktop

**ClearQuery: Analyst for SAP HANA**, by [Dinesh AI Academy](https://dineshaiacademy.com).

## Install

1. Double-click `clearquery-<version>.mcpb`, or open Claude Desktop > **Settings** > **Extensions** and drag the file
   in. Choose **Install**.
2. Fill in the form: HANA host, port, a **read-only** user and its password. The schema is optional.
   - HANA Cloud: the SQL endpoint, port `443`, both TLS switches on.
   - HANA Express: `localhost`, port `39041`, **Validate TLS certificate** off.
3. Make sure the extension is **enabled**. The first start downloads Python and the packages once (about half a
   minute).
4. In a new chat, ask: *"Diagnose the connection, then list my tables."*

Change settings later in **Settings** > **Extensions** > **ClearQuery** > **Configure**.

## Use

Ask in plain words: "Total sales by month last year", "Describe table EKKO", "Why is this query slow?". Claude finds
the tables, checks the real column names, runs read-only queries and shows the SQL when you ask.

## Safety and privacy

ClearQuery only reads: it sends single SELECT / WITH queries and refuses everything else, and you should connect with a
user that can only SELECT. It connects only to the host you enter and sends nothing to Dinesh AI Academy. Your
password is kept in Claude Desktop's secure storage.

Privacy policy: <https://dineshaiacademy.com/privacy-policy.html> · Support: support@dineshaiacademy.com

SAP and SAP HANA are trademarks of SAP SE. ClearQuery is an independent product of Dinesh AI Academy and is not
affiliated with, sponsored by or endorsed by SAP SE or Anthropic. MIT License; see `LICENSE` and `NOTICE.md`.
