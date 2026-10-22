# Alexa+ simulated experience

- **What is simulated:** a voice-first chat layer that recognises the phrase “Alexa, ask CampusPilot to …”, strips it, and sends the rest to the existing `/chat` endpoint.
- **Not connected:** this is not a live Alexa skill, Amazon service, or MCP server; no Amazon logos, trademarks, or assets are used.
- **Source:** `frontend/campuspilot_ui.html` — the “Alexa+ (simulated)” toggle, Talk button, wake-phrase parser, and listening ring.
- **Voice in:** the browser’s existing `SpeechRecognition`/`webkitSpeechRecognition` code; typed text works with the same parser.
- **Voice out:** the browser’s existing `speechSynthesis`; replies are also rendered as normal agent cards with tool chips.
- **Behaviour:** a final utterance is sent automatically, while text input still goes through the normal Send button.
- **Demo:** open the Chat tab, enable “Alexa+ (simulated)”, click Talk, and say “Alexa, ask CampusPilot to plan my next two days”.
- **Fallback:** with no Gemini key the reply comes from the deterministic quick-mode router and is clearly labelled as fallback.
- **Privacy:** audio is handled by the browser speech engine; this app stores no recordings.
- **Limit:** the wake phrase is parsed after recognition, not detected while idle; microphone permission and browser support are required.
