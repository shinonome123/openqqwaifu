# Open Source References

This scaffold references the architecture of these upstream projects:

- `LangBot` (`Apache-2.0`)
- `Waifu` by ElvisChenML (`AGPL-3.0`)
- `NapCatQQ` interface and deployment docs for sidecar wiring only

What is reused here:

- The high-level separation between runtime boundary and business logic
- The `cells / organs / systems` naming pattern from the upstream Waifu project
- The idea of keeping QQ protocol transport outside the main business core
- The OneBot-facing NapCat integration shape: inbound event push plus outbound action API

What is not copied here:

- Upstream implementation code
- Upstream protocol internals
- Upstream prompts, business rules, or data files verbatim
- NapCat protocol-side source code

This local scaffold is intentionally minimal and test-oriented.
