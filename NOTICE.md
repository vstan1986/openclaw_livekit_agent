# License Notices

This project incorporates components from several open-source projects.
We thank their authors for making this work possible.

## MIT License Components

This project itself is licensed under the MIT License — see [LICENSE](LICENSE).

## Apache 2.0 Components

The following runtime dependencies are licensed under Apache 2.0:

- **[livekit-agents](https://github.com/livekit/agents)** — LiveKit Agents SDK
  Copyright LiveKit Inc.
- **[livekit-plugins-openai](https://github.com/livekit/agents)** — LiveKit OpenAI plugin
  Copyright LiveKit Inc.
- **[requests](https://github.com/psf/requests)** — HTTP library
  Copyright 2019 Kenneth Reitz
- **[grpcio](https://github.com/grpc/grpc)** — gRPC framework
  Copyright The gRPC Authors
- **[protobuf](https://github.com/protocolbuffers/protobuf)** — Protocol Buffers
  Copyright Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use these files except in compliance with the License.
You may obtain a copy of the License at:

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

## LGPL Components

- **[num2words](https://github.com/savoirfairelinux/num2words)** — Convert numbers to words
  Licensed under LGPL. See: https://github.com/savoirfairelinux/num2words

## Silero TTS Model Weights (Non-Commercial Restriction)

The TTS microservice (`tts-service`) downloads model weights from
[snakers4/silero-models](https://github.com/snakers4/silero-models) at runtime.

These model weights are licensed under **CC BY-NC-SA 4.0**
(Attribution-NonCommercial-ShareAlike 4.0 International), which restricts
their use to **non-commercial purposes only**.

If you need a TTS engine for commercial use, please replace the TTS service
with an appropriately licensed alternative (e.g., Sber SaluteSpeech TTS API,
OpenAI TTS API, or another commercial TTS solution).

See: https://creativecommons.org/licenses/by-nc-sa/4.0/
