# Personal Discord Voice Assistant

A local, experimental voice assistant for personal use on owned or private Discord servers.

## Summary

This project is intended for legitimate, consent-based experimentation in environments where the operator has permission to run and operate the software. It is designed for personal automation and voice interaction scenarios, not for monitoring, spying, covert audio capture, or unauthorized processing of user speech.

Use responsibly and in compliance with Discord policies, privacy requirements, and local laws.

## Overview

- joins a voice channel
- listens for speech
- transcribes spoken input
- sends text to Gemini for conversational reply generation
- converts the response into voice via TTS
- plays the generated audio back into the voice channel
- supports multiple TTS providers, including Edge TTS, ElevenLabs, and OpenAI

## Responsible usage

This project must be used only in environments where you are authorized to operate it and where participants are aware of the setup. It is not intended for:

- monitoring users without consent
- covert audio capture or spying
- unauthorized processing of voice data
- use on third-party community servers without proper authorization and compliance

## Features

- voice channel join and listen mode
- speech recognition via Google Speech Recognition
- Gemini-based conversational reply generation
- configurable provider priority for TTS
- audio playback into Discord voice channels
- Docker support for deployment

## Requirements

- Python 3.10+
- FFmpeg installed on the system
- Discord token for your own authorized account/server
- Gemini API key
- Optional: ElevenLabs API key and voice ID
- Optional: OpenAI API key

## Quick start

1. Clone the repository.
2. Create a local environment.
3. Copy `.env.example` to `.env` and fill in your values.
4. Install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate   # Linux/macOS
# or .venv\Scripts\Activate.ps1  # Windows PowerShell
pip install -r requirements.txt
```

5. Start the bot:

```bash
python voice_bot.py
```

## Environment variables

See [.env.example](.env.example) for examples.

## Security and compliance

- Keep `.env` local and never commit it to Git.
- Do not store secrets in source control.
- Use on servers where you have permission and consent from participants.
- Do not use this project for unauthorized recording, monitoring, or abusive automation.

## Docker

```bash
docker build -t voice-bot .
docker run --rm -it --env-file .env voice-bot
```

## License

This project is distributed under the MIT License. See [LICENSE](LICENSE).
