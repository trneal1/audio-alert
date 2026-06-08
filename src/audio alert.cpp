/*
  ESP32-S3 TCP audio playback receiver.

  Hardware example: ESP32-S3 -> MAX98357A I2S amp

  ESP32 GPIO     MAX98357A
  -------------------------
  GPIO 1    ->   BCLK
  GPIO 2    ->   LRC / WS
  GPIO 3    ->   DIN
  5V/VIN    ->   VIN
  GND       ->   GND

  TCP protocol, version AUD1:

    Byte  0..3   magic: "AUD1"
    Byte  4..7   sample rate, uint32 little-endian. Supported: 8000..48000.
    Byte  8      channels. Supported: 1 mono or 2 stereo.
    Byte  9      bits per sample. Supported: 16.
    Byte 10      audio format. Supported: 1 = signed PCM little-endian.
    Byte 11      flags. Reserved, send 0.
    Byte 12..15  payload byte count, uint32 little-endian.
                 Send 0 to stream until the TCP connection closes.

    The device replies with "OK AUD1\n" before audio bytes are consumed, or
    "ERR <reason>\n" and closes the connection.

  Python sender sketch:

    import socket, struct
    hdr = b"AUD1" + struct.pack("<IBBBBI", 24000, 1, 16, 1, 0, len(pcm))
    with socket.create_connection(("audio-alert.local", 7777)) as s:
        s.sendall(hdr)
        print(s.recv(64).decode())
        s.sendall(pcm)
        print(s.recv(64).decode())
*/

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include "driver/i2s.h"

// ---------- Wi-Fi ----------
const char* WIFI_SSID = "TRNNET-2G";
const char* WIFI_PASS = "ripcord1";
const char* WIFI_HOSTNAME = "audio-alert";

// ---------- TCP server ----------
const uint16_t TCP_PORT = 7777;
WiFiServer server(TCP_PORT);

// ---------- I2S pins ----------
const int I2S_BCLK = 1;
const int I2S_LRC = 2;
const int I2S_DOUT = 3;
const i2s_port_t I2S_PORT = I2S_NUM_0;

// ---------- Audio protocol ----------
const uint32_t PROTOCOL_MAGIC = 0x31445541;  // "AUD1" as little-endian uint32
const uint8_t AUDIO_FORMAT_PCM16_LE = 1;
const uint32_t MIN_SAMPLE_RATE = 8000;
const uint32_t MAX_SAMPLE_RATE = 48000;
const uint32_t CLIENT_IDLE_TIMEOUT_MS = 5000;
const uint32_t PREBUFFER_MS = 1000;

struct AudioHeader {
  uint32_t magic;
  uint32_t sampleRate;
  uint8_t channels;
  uint8_t bitsPerSample;
  uint8_t format;
  uint8_t flags;
  uint32_t payloadBytes;
};

uint32_t readU32LE(const uint8_t* data) {
  return static_cast<uint32_t>(data[0]) |
         (static_cast<uint32_t>(data[1]) << 8) |
         (static_cast<uint32_t>(data[2]) << 16) |
         (static_cast<uint32_t>(data[3]) << 24);
}

bool readExact(WiFiClient& client, uint8_t* buffer, size_t length, uint32_t timeoutMs) {
  size_t received = 0;
  uint32_t lastByteAt = millis();

  while (received < length && client.connected()) {
    int available = client.available();

    if (available > 0) {
      int chunk = client.read(buffer + received, length - received);
      if (chunk > 0) {
        received += static_cast<size_t>(chunk);
        lastByteAt = millis();
      }
    } else {
      if (millis() - lastByteAt > timeoutMs) {
        return false;
      }
      delay(1);
    }
  }

  return received == length;
}

bool readHeader(WiFiClient& client, AudioHeader& header) {
  uint8_t raw[16];

  if (!readExact(client, raw, sizeof(raw), CLIENT_IDLE_TIMEOUT_MS)) {
    return false;
  }

  header.magic = readU32LE(raw + 0);
  header.sampleRate = readU32LE(raw + 4);
  header.channels = raw[8];
  header.bitsPerSample = raw[9];
  header.format = raw[10];
  header.flags = raw[11];
  header.payloadBytes = readU32LE(raw + 12);

  return true;
}

const char* validateHeader(const AudioHeader& header) {
  if (header.magic != PROTOCOL_MAGIC) return "bad magic";
  if (header.sampleRate < MIN_SAMPLE_RATE || header.sampleRate > MAX_SAMPLE_RATE) return "unsupported sample rate";
  if (header.channels != 1 && header.channels != 2) return "unsupported channel count";
  if (header.bitsPerSample != 16) return "unsupported bits per sample";
  if (header.format != AUDIO_FORMAT_PCM16_LE) return "unsupported audio format";
  if (header.flags != 0) return "unsupported flags";

  return nullptr;
}

void setupI2S(uint32_t sampleRate) {
  i2s_driver_uninstall(I2S_PORT);

  i2s_config_t config = {};
  config.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX);
  config.sample_rate = sampleRate;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  config.dma_buf_count = 8;
  config.dma_buf_len = 256;
  config.use_apll = false;
  config.tx_desc_auto_clear = true;
  config.fixed_mclk = 0;

  i2s_pin_config_t pins = {};
  pins.bck_io_num = I2S_BCLK;
  pins.ws_io_num = I2S_LRC;
  pins.data_out_num = I2S_DOUT;
  pins.data_in_num = I2S_PIN_NO_CHANGE;

  esp_err_t result = i2s_driver_install(I2S_PORT, &config, 0, nullptr);
  if (result != ESP_OK) {
    Serial.print("i2s_driver_install failed: ");
    Serial.println(result);
  }

  result = i2s_set_pin(I2S_PORT, &pins);
  if (result != ESP_OK) {
    Serial.print("i2s_set_pin failed: ");
    Serial.println(result);
  }

  i2s_zero_dma_buffer(I2S_PORT);
}

bool writeStereoSamples(const uint8_t* input, size_t inputBytes, uint8_t channels) {
  size_t bytesWritten = 0;

  if (channels == 2) {
    return i2s_write(I2S_PORT, input, inputBytes, &bytesWritten, portMAX_DELAY) == ESP_OK &&
           bytesWritten == inputBytes;
  }

  int16_t stereo[512];
  const int16_t* mono = reinterpret_cast<const int16_t*>(input);
  const size_t sampleCount = inputBytes / sizeof(int16_t);
  size_t sampleIndex = 0;

  while (sampleIndex < sampleCount) {
    size_t frames = min(sampleCount - sampleIndex, sizeof(stereo) / (2 * sizeof(int16_t)));

    for (size_t i = 0; i < frames; i++) {
      int16_t sample = mono[sampleIndex + i];
      stereo[i * 2] = sample;
      stereo[i * 2 + 1] = sample;
    }

    size_t outputBytes = frames * 2 * sizeof(int16_t);
    bytesWritten = 0;
    if (i2s_write(I2S_PORT, stereo, outputBytes, &bytesWritten, portMAX_DELAY) != ESP_OK ||
        bytesWritten != outputBytes) {
      return false;
    }

    sampleIndex += frames;
  }

  return true;
}

size_t audioFrameBytes(const AudioHeader& header) {
  return static_cast<size_t>(header.channels) * sizeof(int16_t);
}

uint32_t prebufferBytesFor(const AudioHeader& header) {
  uint32_t bytes = header.sampleRate * header.channels * sizeof(int16_t) * PREBUFFER_MS / 1000;
  uint32_t frameBytes = static_cast<uint32_t>(audioFrameBytes(header));

  bytes -= bytes % frameBytes;
  if (header.payloadBytes > 0 && bytes > header.payloadBytes) {
    bytes = header.payloadBytes - (header.payloadBytes % frameBytes);
  }

  return bytes;
}

bool readAudioBytes(WiFiClient& client, uint8_t* buffer, size_t length, uint32_t& lastByteAt) {
  size_t received = 0;

  while (received < length && client.connected()) {
    int available = client.available();

    if (available > 0) {
      size_t wanted = min(static_cast<size_t>(available), length - received);
      int bytesRead = client.read(buffer + received, wanted);
      if (bytesRead > 0) {
        received += static_cast<size_t>(bytesRead);
        lastByteAt = millis();
        continue;
      }
    }

    if (millis() - lastByteAt > CLIENT_IDLE_TIMEOUT_MS) {
      Serial.println("Client audio stream timed out");
      return false;
    }
    delay(1);
  }

  return received == length;
}

bool playAudioStream(WiFiClient& client, const AudioHeader& header) {
  uint8_t buffer[2048];
  uint32_t remaining = header.payloadBytes;
  uint32_t lastByteAt = millis();
  uint32_t playedBytes = 0;
  uint32_t prebufferBytes = prebufferBytesFor(header);

  if (prebufferBytes > 0) {
    uint8_t* prebuffer = static_cast<uint8_t*>(malloc(prebufferBytes));
    if (prebuffer == nullptr) {
      Serial.println("Audio prebuffer allocation failed");
      return false;
    }

    Serial.print("Prebuffering PCM bytes: ");
    Serial.println(prebufferBytes);

    bool ok = readAudioBytes(client, prebuffer, prebufferBytes, lastByteAt);
    if (ok) {
      ok = writeStereoSamples(prebuffer, prebufferBytes, header.channels);
    }
    free(prebuffer);

    if (!ok) {
      Serial.println("Prebuffer playback failed");
      return false;
    }

    playedBytes += prebufferBytes;
    if (header.payloadBytes > 0) {
      remaining -= prebufferBytes;
    }
  }

  while (client.connected()) {
    size_t wanted = sizeof(buffer);

    if (header.payloadBytes > 0) {
      if (remaining == 0) {
        break;
      }
      wanted = min(static_cast<uint32_t>(sizeof(buffer)), remaining);
    }

    int available = client.available();
    if (available <= 0) {
      if (millis() - lastByteAt > CLIENT_IDLE_TIMEOUT_MS) {
        Serial.println("Client audio stream timed out");
        return false;
      }
      delay(1);
      continue;
    }

    size_t toRead = min(static_cast<size_t>(available), wanted);

    if (header.channels == 1) {
      toRead &= ~static_cast<size_t>(1);
    } else {
      toRead &= ~static_cast<size_t>(3);
    }

    if (toRead == 0) {
      delay(1);
      continue;
    }

    int bytesRead = client.read(buffer, toRead);
    if (bytesRead <= 0) {
      delay(1);
      continue;
    }

    lastByteAt = millis();

    if (!writeStereoSamples(buffer, static_cast<size_t>(bytesRead), header.channels)) {
      Serial.println("I2S write failed");
      return false;
    }

    playedBytes += static_cast<uint32_t>(bytesRead);
    if (header.payloadBytes > 0) {
      remaining -= static_cast<uint32_t>(bytesRead);
    }
  }

  i2s_zero_dma_buffer(I2S_PORT);

  Serial.print("Played PCM bytes: ");
  Serial.println(playedBytes);

  return header.payloadBytes == 0 || remaining == 0;
}

void handleClient(WiFiClient client) {
  Serial.println("TCP client connected");
  client.setNoDelay(true);
  client.setTimeout(CLIENT_IDLE_TIMEOUT_MS);

  AudioHeader header;
  if (!readHeader(client, header)) {
    client.println("ERR header timeout");
    client.stop();
    Serial.println("TCP client disconnected during header");
    return;
  }

  const char* error = validateHeader(header);
  if (error != nullptr) {
    client.print("ERR ");
    client.println(error);
    client.stop();
    Serial.print("Rejected audio stream: ");
    Serial.println(error);
    return;
  }

  Serial.print("Audio stream: ");
  Serial.print(header.sampleRate);
  Serial.print(" Hz, ");
  Serial.print(header.channels);
  Serial.print(" ch, bytes=");
  Serial.println(header.payloadBytes == 0 ? String("streaming") : String(header.payloadBytes));

  setupI2S(header.sampleRate);
  client.println("OK AUD1");

  bool ok = playAudioStream(client, header);
  client.println(ok ? "DONE" : "ERR playback failed");
  client.stop();
  Serial.println("TCP client disconnected");
}

void setupWiFi() {
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(WIFI_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("Wi-Fi connected");
  Serial.print("Hostname: ");
  Serial.println(WIFI_HOSTNAME);
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());

  if (MDNS.begin(WIFI_HOSTNAME)) {
    MDNS.addService("audio", "tcp", TCP_PORT);
    Serial.print("mDNS name: ");
    Serial.print(WIFI_HOSTNAME);
    Serial.println(".local");
  } else {
    Serial.println("mDNS setup failed");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);

  setupI2S(24000);
  setupWiFi();

  server.begin();

  Serial.print("TCP audio server listening on port ");
  Serial.println(TCP_PORT);
}

void loop() {
  WiFiClient client = server.accept();
  if (client) {
    handleClient(client);
  }

  delay(1);
}
