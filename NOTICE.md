# Notice: brand, trademarks and third-party software

**ClearQuery** is built by **Dinesh AI Academy** (https://dineshaiacademy.com). Contact: support@dineshaiacademy.com.
Privacy policy: https://dineshaiacademy.com/privacy-policy.html

## License

The plugin's own code, skill and documentation are under the MIT License (see `LICENSE`): free for personal and
commercial use. The only condition is to keep the copyright and license notice.

## Brand

The names **Dinesh AI Academy** and **ClearQuery**, the "DA" logo (`assets/icon.png`) and the tagline are trademarks of
Dinesh AI Academy. The MIT License does not grant rights to them. You may say that this plugin is "built by Dinesh AI
Academy", and keep the name and logo in an unmodified copy. If you publish a modified version, give it a name, icon and
author of your own, keep the MIT notice, and do not suggest that Dinesh AI Academy endorses it.

## Third-party software (installed on your computer, not included in this folder)

When the plugin starts, `uv run --locked` installs the exact packages pinned in `uv.lock` from PyPI. Each keeps its own
license, which you can read in the package after installation:

| Package | Used for | License |
|---|---|---|
| `mcp` (MCP Python SDK) 2.3.0 and its dependencies | talking to Claude | MIT and other open-source licenses |
| `hdbcli` 2.30.27 (SAP HANA client for Python) | connecting to SAP HANA | **SAP Developer License Agreement** (SAP's own terms, not open source) |

`hdbcli` is SAP's software. It is not part of this repository or this plugin's files; it is downloaded to your computer,
and by running the plugin you use it under SAP's terms. Read them before using it in production.

## Trademarks

SAP and SAP HANA are trademarks or registered trademarks of SAP SE. Claude and Anthropic are trademarks of Anthropic,
PBC. ClearQuery is an independent product of Dinesh AI Academy and is not affiliated with, sponsored by or endorsed by
SAP SE or Anthropic. The names SAP and SAP HANA are used only to say which database this plugin works with.

This notice is a practical summary, not legal advice.
