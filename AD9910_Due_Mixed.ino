/*
  Mixed-clock version: clock mode is independent for every DDS.
  Arduino Due controller for 1..20 AD9910 channels and the companion GUI.

  External numbering is consistently 1..20. Internal arrays are zero-based.
  Use the Programming Port and Serial at 115200 baud.

  Shared signals:
    SPI header COPI -> all SDIO; SPI header SCK -> all SCK
    D25 -> all IO_UPDATE; D24 -> all RESET; all grounds common

  DDS 1..20 CSB pins:
    35,37,39,41,47,49,51,53,52,50,48,46,44,42,40,38,36,34,32,30
  D31, D33, D43, and D45 are externally grounded and never driven.

  Shutter 1..20 pins:
    D2..D11, D14..D21, D22, D23
  LOW closes/blocks and HIGH opens. Each has an external 10 kohm pull-down.

  PF0/PF1/PF2 are grounded for Profile 0. Two clock modes are supported while
  retaining 1 GHz SYSCLK and SPI mode 0 at 10 MHz:
    INTERNAL: module 40 MHz reference, PLL x25, CFR3=0x0538C132
    EXTERNAL: direct external 1 GHz reference, PLL off, CFR3=0x073FC000
  CFR1=0x00000002 and CFR2=0x01000020 in both modes.

  Startup leaves every CSB HIGH and every shutter LOW until CONFIG is received.
  CONFIG <count> initializes DDS 1..count to F=100 MHz, A=0, P=0 and then opens
  their Internal Shutters. The remainder stay closed and isolated with CSB HIGH.
  F=0 always writes effective ASF=0.

  Commands (ASCII, newline terminated):
    HELLO | CONFIG <1..20> <I/E map>
    F <dds> <0..499999999> | A <dds> <0..1>
    P <dds> <0..359.999999> | SHUTTER <n> <0|1> | APPLY ALL
    STATUS [dds] | RESET | HELP
  F/A/P are staged until the common APPLY ALL. SHUTTER is immediate.
  Every response begins with READY, OK, ERR, or DATA.
*/

#include <SPI.h>
#include <ctype.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

constexpr uint8_t PIN_AD9910_IOUP = 25;
constexpr uint8_t PIN_AD9910_RESET = 24;
constexpr uint8_t CS_PINS[] = {
  35, 37, 39, 41, 47, 49, 51, 53, 52, 50,
  48, 46, 44, 42, 40, 38, 36, 34, 32, 30
};
constexpr uint8_t SHUTTER_PINS[] = {
  2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
  14, 15, 16, 17, 18, 19, 20, 21, 22, 23
};
constexpr size_t NUM_CHANNELS = sizeof(CS_PINS) / sizeof(CS_PINS[0]);
static_assert(NUM_CHANNELS == 20, "Exactly 20 CS pins are required");
static_assert(sizeof(SHUTTER_PINS) / sizeof(SHUTTER_PINS[0]) == NUM_CHANNELS,
              "Each DDS must have one shutter pin");

constexpr uint32_t INTERNAL_REF_CLK_HZ = 40000000UL;
constexpr uint32_t EXTERNAL_REF_CLK_HZ = 1000000000UL;
constexpr uint8_t PLL_MULTIPLIER = 25;
constexpr uint8_t PLL_VCO_SELECT = 5;
constexpr uint8_t PLL_CHARGE_PUMP = 7;
constexpr uint16_t PLL_LOCK_WAIT_MS = 10;
constexpr uint64_t SYSCLK_CALCULATED_HZ =
    static_cast<uint64_t>(INTERNAL_REF_CLK_HZ) * PLL_MULTIPLIER;
static_assert(INTERNAL_REF_CLK_HZ >= 3200000UL && INTERNAL_REF_CLK_HZ <= 60000000UL,
              "PLL reference input must be 3.2..60 MHz");
static_assert(PLL_MULTIPLIER >= 12 && PLL_MULTIPLIER <= 127,
              "AD9910 PLL multiplier must be 12..127");
static_assert(SYSCLK_CALCULATED_HZ >= 420000000ULL &&
              SYSCLK_CALCULATED_HZ <= 1000000000ULL,
              "PLL SYSCLK must be 420 MHz..1 GHz");
constexpr uint32_t SYSCLK_HZ = static_cast<uint32_t>(SYSCLK_CALCULATED_HZ);
constexpr uint32_t INITIAL_FREQUENCY_HZ = 100000000UL;
constexpr uint32_t MAX_FREQUENCY_HZ = 499999999UL;
constexpr uint32_t SPI_CLOCK_HZ = 10000000UL;
const SPISettings AD9910_SPI_SETTINGS(SPI_CLOCK_HZ, MSBFIRST, SPI_MODE0);

constexpr uint8_t REG_CFR1 = 0x00;
constexpr uint8_t REG_CFR2 = 0x01;
constexpr uint8_t REG_CFR3 = 0x02;
constexpr uint8_t REG_PROFILE0 = 0x0E;
constexpr uint32_t CFR1_VALUE = 0x00000002UL;
constexpr uint32_t CFR2_VALUE = 0x01000020UL;
constexpr uint32_t CFR3_INTERNAL_VALUE =
    (static_cast<uint32_t>(PLL_VCO_SELECT) << 24) |
    (static_cast<uint32_t>(PLL_CHARGE_PUMP) << 19) |
    (1UL << 15) | (1UL << 14) | (1UL << 8) |
    (static_cast<uint32_t>(PLL_MULTIPLIER) << 1);
constexpr uint32_t CFR3_EXTERNAL_VALUE = 0x073FC000UL;
static_assert(CFR3_INTERNAL_VALUE == 0x0538C132UL,
              "Unexpected CFR3 for 40 MHz x25 clock plan");

enum class ClockMode : uint8_t { INTERNAL_PLL, EXTERNAL_DIRECT };
// One hardware clock choice per physical DDS; zero-initialized to INTERNAL_PLL.
ClockMode channelClockModes[NUM_CHANNELS] = {};

const char *clockModeName(uint8_t channel) {
  return channelClockModes[channel] == ClockMode::INTERNAL_PLL ? "INTERNAL" : "EXTERNAL";
}

uint32_t selectedCfr3(uint8_t channel) {
  return channelClockModes[channel] == ClockMode::INTERNAL_PLL
      ? CFR3_INTERNAL_VALUE : CFR3_EXTERNAL_VALUE;
}

uint32_t selectedReferenceHz(uint8_t channel) {
  return channelClockModes[channel] == ClockMode::INTERNAL_PLL
      ? INTERNAL_REF_CLK_HZ : EXTERNAL_REF_CLK_HZ;
}

struct ChannelState {
  uint32_t frequencyHz;
  uint16_t asf;
  uint16_t phaseWord;
  bool shutterOpen;
  bool dirty;
};

ChannelState channelState[NUM_CHANNELS];
uint8_t activeChannelCount = 0;
char serialBuffer[112];
size_t serialLength = 0;

bool validPhysicalChannel(uint8_t channel) { return channel < NUM_CHANNELS; }
bool activeChannel(uint8_t channel) { return channel < activeChannelCount; }

void deselectAllChannels() {
  for (size_t ch = 0; ch < NUM_CHANNELS; ++ch) digitalWrite(CS_PINS[ch], HIGH);
}

void closeAllShutters() {
  for (size_t ch = 0; ch < NUM_CHANNELS; ++ch) {
    digitalWrite(SHUTTER_PINS[ch], LOW);
    channelState[ch].shutterOpen = false;
  }
}

void loadInitialChannelStates() {
  for (size_t ch = 0; ch < NUM_CHANNELS; ++ch) {
    channelState[ch] = {INITIAL_FREQUENCY_HZ, 0, 0, false, false};
  }
}

bool writeRegister(uint8_t channel, uint8_t address,
                   const uint8_t *data, size_t length) {
  if (!activeChannel(channel) || data == nullptr || length == 0) return false;
  SPI.beginTransaction(AD9910_SPI_SETTINGS);
  deselectAllChannels();
  digitalWrite(CS_PINS[channel], LOW);
  SPI.transfer(address & 0x1F);
  for (size_t i = 0; i < length; ++i) SPI.transfer(data[i]);
  digitalWrite(CS_PINS[channel], HIGH);
  SPI.endTransaction();
  return true;
}

bool writeRegister32(uint8_t channel, uint8_t address, uint32_t value) {
  const uint8_t data[4] = {
    static_cast<uint8_t>(value >> 24), static_cast<uint8_t>(value >> 16),
    static_cast<uint8_t>(value >> 8), static_cast<uint8_t>(value)
  };
  return writeRegister(channel, address, data, sizeof(data));
}

bool writeProfile(uint8_t channel, uint32_t ftw,
                  uint16_t phaseWord, uint16_t asf) {
  if (!activeChannel(channel)) return false;
  asf &= 0x3FFF;
  const uint8_t data[8] = {
    static_cast<uint8_t>((asf >> 8) & 0x3F), static_cast<uint8_t>(asf),
    static_cast<uint8_t>(phaseWord >> 8), static_cast<uint8_t>(phaseWord),
    static_cast<uint8_t>(ftw >> 24), static_cast<uint8_t>(ftw >> 16),
    static_cast<uint8_t>(ftw >> 8), static_cast<uint8_t>(ftw)
  };
  return writeRegister(channel, REG_PROFILE0, data, sizeof(data));
}

void pulseIoUpdate() {
  digitalWrite(PIN_AD9910_IOUP, HIGH);
  delayMicroseconds(1);
  digitalWrite(PIN_AD9910_IOUP, LOW);
  delayMicroseconds(1);
  for (size_t ch = 0; ch < activeChannelCount; ++ch) channelState[ch].dirty = false;
}

void resetAllHardware() {
  deselectAllChannels();
  digitalWrite(PIN_AD9910_RESET, LOW);
  delayMicroseconds(1);
  digitalWrite(PIN_AD9910_RESET, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_AD9910_RESET, LOW);
  delay(1);
}

uint32_t frequencyToFtw(uint32_t frequencyHz) {
  const uint64_t numerator = static_cast<uint64_t>(frequencyHz) * (1ULL << 32);
  return static_cast<uint32_t>((numerator + SYSCLK_HZ / 2ULL) / SYSCLK_HZ);
}
uint16_t amplitudeToAsf(double amplitude) {
  return static_cast<uint16_t>(lround(amplitude * 16383.0));
}
uint16_t degreesToPhaseWord(double degrees) {
  const uint32_t rounded = static_cast<uint32_t>(lround(degrees * 65536.0 / 360.0));
  return static_cast<uint16_t>(rounded & 0xFFFFUL);
}
double asfToAmplitude(uint16_t asf) { return static_cast<double>(asf) / 16383.0; }
double phaseWordToDegrees(uint16_t word) {
  return static_cast<double>(word) * 360.0 / 65536.0;
}

bool stageCurrentProfile(uint8_t channel) {
  if (!activeChannel(channel)) return false;
  ChannelState &state = channelState[channel];
  const uint16_t effectiveAsf = state.frequencyHz == 0 ? 0 : state.asf;
  if (!writeProfile(channel, frequencyToFtw(state.frequencyHz),
                    state.phaseWord, effectiveAsf)) return false;
  state.dirty = true;
  return true;
}

bool setFrequency(uint8_t channel, uint32_t frequencyHz) {
  if (!activeChannel(channel) || frequencyHz > MAX_FREQUENCY_HZ) return false;
  channelState[channel].frequencyHz = frequencyHz;
  return stageCurrentProfile(channel);
}
bool setAmplitude(uint8_t channel, double amplitude) {
  if (!activeChannel(channel) || !isfinite(amplitude) ||
      amplitude < 0.0 || amplitude > 1.0) return false;
  channelState[channel].asf = amplitudeToAsf(amplitude);
  return stageCurrentProfile(channel);
}
bool setPhase(uint8_t channel, double degrees) {
  if (!activeChannel(channel) || !isfinite(degrees) ||
      degrees < 0.0 || degrees >= 360.0) return false;
  channelState[channel].phaseWord = degreesToPhaseWord(degrees);
  return stageCurrentProfile(channel);
}
bool setShutter(uint8_t channel, bool open) {
  if (!activeChannel(channel)) return false;
  digitalWrite(SHUTTER_PINS[channel], open ? HIGH : LOW);
  channelState[channel].shutterOpen = open;
  return true;
}

bool initializeChannel(uint8_t channel) {
  return activeChannel(channel) &&
         writeRegister32(channel, REG_CFR1, CFR1_VALUE) &&
         writeRegister32(channel, REG_CFR2, CFR2_VALUE) &&
         writeRegister32(channel, REG_CFR3, selectedCfr3(channel)) &&
         stageCurrentProfile(channel);
}

bool configureChannels(uint8_t count, const ClockMode *modes) {
  if (count == 0 || count > NUM_CHANNELS) return false;
  closeAllShutters();
  deselectAllChannels();
  loadInitialChannelStates();
  activeChannelCount = count;
  // The complete map is validated before any reset or hardware write.
  for (uint8_t ch = 0; ch < count; ++ch) channelClockModes[ch] = modes[ch];
  resetAllHardware();
  for (uint8_t ch = 0; ch < activeChannelCount; ++ch) {
    if (!initializeChannel(ch)) {
      closeAllShutters();
      deselectAllChannels();
      activeChannelCount = 0;
      return false;
    }
  }
  pulseIoUpdate();
  delay(PLL_LOCK_WAIT_MS); // Allow any internally clocked DDS to settle.
  for (uint8_t ch = 0; ch < activeChannelCount; ++ch) {
    setShutter(ch, true);
  }
  deselectAllChannels();
  return true;
}

bool resetConfiguredChannels() {
  if (activeChannelCount == 0) {
    closeAllShutters();
    resetAllHardware();
    return true;
  }
  return configureChannels(activeChannelCount, channelClockModes);
}

bool parseUint32(const char *text, uint32_t &value) {
  if (text == nullptr || *text == '\0' || *text == '-') return false;
  char *end = nullptr;
  const unsigned long long parsed = strtoull(text, &end, 10);
  if (end == text || *end != '\0' || parsed > 0xFFFFFFFFULL) return false;
  value = static_cast<uint32_t>(parsed);
  return true;
}
bool parseDoubleValue(const char *text, double &value) {
  if (text == nullptr || *text == '\0') return false;
  char *end = nullptr;
  const double parsed = strtod(text, &end);
  if (end == text || *end != '\0' || !isfinite(parsed)) return false;
  value = parsed;
  return true;
}
bool parseUserChannel(const char *text, uint8_t &channel) {
  uint32_t number = 0;
  if (!parseUint32(text, number) || number == 0 || number > NUM_CHANNELS) return false;
  channel = static_cast<uint8_t>(number - 1);
  return true;
}
void uppercase(char *text) {
  if (text == nullptr) return;

  while (*text != '\0') {
    *text = static_cast<char>(
        toupper(static_cast<unsigned char>(*text)));
    ++text;
  }
}
void replyError(const char *code, const char *message) {
  Serial.print("ERR "); Serial.print(code); Serial.print(' '); Serial.println(message);
}

void printChannelData(uint8_t channel) {
  if (!validPhysicalChannel(channel)) return;
  const ChannelState &s = channelState[channel];
  Serial.print("DATA CH "); Serial.print(channel + 1);
  Serial.print(" ENABLED="); Serial.print(activeChannel(channel) ? 1 : 0);
  Serial.print(" CLOCK="); Serial.print(clockModeName(channel));
  Serial.print(" REFCLK="); Serial.print(selectedReferenceHz(channel));
  Serial.print(" CFR3=0x"); Serial.print(selectedCfr3(channel), HEX);
  Serial.print(" CS="); Serial.print(CS_PINS[channel]);
  Serial.print(" SHUTTER_PIN="); Serial.print(SHUTTER_PINS[channel]);
  Serial.print(" F="); Serial.print(s.frequencyHz);
  Serial.print(" A="); Serial.print(asfToAmplitude(s.asf), 6);
  Serial.print(" P="); Serial.print(phaseWordToDegrees(s.phaseWord), 6);
  Serial.print(" SHUTTER="); Serial.print(s.shutterOpen ? 1 : 0);
  Serial.print(" STATE="); Serial.println(s.dirty ? "STAGED" : "APPLIED");
}

void printSystemData() {
  Serial.print("DATA SYSTEM ACTIVE="); Serial.print(activeChannelCount);
  Serial.print(" CLOCKS=");
  for (uint8_t ch = 0; ch < activeChannelCount; ++ch)
    Serial.print(channelClockModes[ch] == ClockMode::INTERNAL_PLL ? 'I' : 'E');
  Serial.print(" SYSCLK="); Serial.print(SYSCLK_HZ);
  Serial.print(" SPI="); Serial.print(SPI_CLOCK_HZ);
  Serial.print(" CFR1=0x"); Serial.print(CFR1_VALUE, HEX);
  Serial.print(" CFR2=0x"); Serial.print(CFR2_VALUE, HEX);
  Serial.println();
}

void printHelp() {
  Serial.println("DATA HELP HELLO");
  Serial.println("DATA HELP CONFIG <1..20> <I/E map>");
  Serial.println("DATA HELP F <dds> <0..499999999>");
  Serial.println("DATA HELP A <dds> <0..1>");
  Serial.println("DATA HELP P <dds> <0..359.999999>");
  Serial.println("DATA HELP SHUTTER <n> <0|1>");
  Serial.println("DATA HELP APPLY ALL");
  Serial.println("DATA HELP STATUS [dds]");
  Serial.println("DATA HELP RESET");
  Serial.println("OK HELP");
}

void handleSerialLine() {
  serialBuffer[serialLength] = '\0';
  char *save = nullptr;
  char *command = strtok_r(serialBuffer, " \t", &save);
  if (command == nullptr) return;
  uppercase(command);

  if (strcmp(command, "HELLO") == 0) {
    Serial.println("OK HELLO AD9910_DUE_MIXED PROTOCOL=3");
    printSystemData();
    return;
  }
  if (strcmp(command, "HELP") == 0) { printHelp(); return; }

  if (strcmp(command, "CONFIG") == 0) {
    uint32_t count = 0;
    if (!parseUint32(strtok_r(nullptr, " \t", &save), count) ||
        count == 0 || count > NUM_CHANNELS) {
      replyError("RANGE", "CONFIG count must be 1..20"); return;
    }
    // Compact I/E map fits all 20 clocks in a short serial line.
    // Example: CONFIG 3 IEI -> DDS1 internal, DDS2 external, DDS3 internal.
    char *map = strtok_r(nullptr, " \t", &save);
    if (map == nullptr || strlen(map) != count ||
        strtok_r(nullptr, " \t", &save) != nullptr) {
      replyError("CLOCK", "provide exactly one I/E character per DDS"); return;
    }
    uppercase(map);
    ClockMode modes[NUM_CHANNELS];
    for (uint8_t ch = 0; ch < count; ++ch) {
      if (map[ch] != 'I' && map[ch] != 'E') {
        replyError("CLOCK", "map must contain only I or E"); return;
      }
      modes[ch] = map[ch] == 'I' ? ClockMode::INTERNAL_PLL : ClockMode::EXTERNAL_DIRECT;
    }
    if (!configureChannels(static_cast<uint8_t>(count), modes)) {
      replyError("HW", "DDS configuration failed"); return;
    }
    Serial.print("OK CONFIG ACTIVE="); Serial.print(activeChannelCount);
    Serial.print(" CLOCKS="); Serial.println(map);
    return;
  }

  if (strcmp(command, "APPLY") == 0) {
    if (activeChannelCount == 0) {
      replyError("NOT_CONFIGURED", "send CONFIG first"); return;
    }
    pulseIoUpdate();
    Serial.println("OK APPLY ALL");
    return;
  }

  if (strcmp(command, "RESET") == 0) {
    if (!resetConfiguredChannels()) {
      replyError("HW", "reset or reinitialization failed"); return;
    }
    Serial.print("OK RESET ACTIVE="); Serial.println(activeChannelCount);
    return;
  }

  if (strcmp(command, "STATUS") == 0) {
    char *channelText = strtok_r(nullptr, " \t", &save);
    printSystemData();
    if (channelText == nullptr) {
      for (uint8_t ch = 0; ch < NUM_CHANNELS; ++ch) printChannelData(ch);
      Serial.println("OK STATUS ALL");
      return;
    }
    uint8_t channel = 0;
    if (!parseUserChannel(channelText, channel)) {
      replyError("CHANNEL", "DDS number must be 1..20"); return;
    }
    printChannelData(channel);
    Serial.print("OK STATUS "); Serial.println(channel + 1);
    return;
  }

  uint8_t channel = 0;
  if (!parseUserChannel(strtok_r(nullptr, " \t", &save), channel)) {
    replyError("CHANNEL", "DDS number must be 1..20"); return;
  }
  if (!activeChannel(channel)) {
    replyError("DISABLED", "DDS is outside the configured range"); return;
  }
  char *valueText = strtok_r(nullptr, " \t", &save);
  if (valueText == nullptr) { replyError("ARG", "missing value"); return; }

  if (strcmp(command, "F") == 0) {
    uint32_t value = 0;
    if (!parseUint32(valueText, value) || !setFrequency(channel, value)) {
      replyError("RANGE", "frequency must be 0..499999999 Hz"); return;
    }
    Serial.print("OK F "); Serial.print(channel + 1); Serial.print(' ');
    Serial.print(value); Serial.println(" STAGED"); return;
  }
  if (strcmp(command, "A") == 0) {
    double value = 0.0;
    if (!parseDoubleValue(valueText, value) || !setAmplitude(channel, value)) {
      replyError("RANGE", "amplitude must be 0..1"); return;
    }
    Serial.print("OK A "); Serial.print(channel + 1); Serial.print(' ');
    Serial.print(value, 6); Serial.println(" STAGED"); return;
  }
  if (strcmp(command, "P") == 0) {
    double value = 0.0;
    if (!parseDoubleValue(valueText, value) || !setPhase(channel, value)) {
      replyError("RANGE", "phase must be 0..359.999999 degrees"); return;
    }
    Serial.print("OK P "); Serial.print(channel + 1); Serial.print(' ');
    Serial.print(value, 6); Serial.println(" STAGED"); return;
  }
  if (strcmp(command, "SHUTTER") == 0) {
    uint32_t value = 0;
    if (!parseUint32(valueText, value) || value > 1 ||
        !setShutter(channel, value == 1)) {
      replyError("RANGE", "shutter value must be 0 or 1"); return;
    }
    Serial.print("OK SHUTTER "); Serial.print(channel + 1); Serial.print(' ');
    Serial.println(value); return;
  }
  replyError("COMMAND", "unknown command");
}

void setup() {
  // Set safe latch values before enabling output drivers.
  for (size_t ch = 0; ch < NUM_CHANNELS; ++ch) {
    digitalWrite(CS_PINS[ch], HIGH);
    pinMode(CS_PINS[ch], OUTPUT);
  }
  for (size_t ch = 0; ch < NUM_CHANNELS; ++ch) {
    digitalWrite(SHUTTER_PINS[ch], LOW);
    pinMode(SHUTTER_PINS[ch], OUTPUT);
  }
  digitalWrite(PIN_AD9910_IOUP, LOW);
  digitalWrite(PIN_AD9910_RESET, LOW);
  pinMode(PIN_AD9910_IOUP, OUTPUT);
  pinMode(PIN_AD9910_RESET, OUTPUT);

  loadInitialChannelStates();
  SPI.begin();
  delay(10);
  resetAllHardware();
  Serial.begin(115200);
  Serial.println("READY AD9910_DUE_MIXED PROTOCOL=3 ACTIVE=0 CLOCKS=NONE");
}

void loop() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());

    if (c == '\n' || c == '\r') {
      if (serialLength > 0) {
        handleSerialLine();
        serialLength = 0;
      }
    } else if (serialLength < sizeof(serialBuffer) - 1) {
      serialBuffer[serialLength++] = c;
    } else {
      serialLength = 0;
      replyError("LINE_TOO_LONG", "command discarded");
    }
  }
}
