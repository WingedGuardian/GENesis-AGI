#pragma once

#include "esphome.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/automation.h"
#ifdef USE_ESP_IDF
#include "esp_websocket_client.h"
#include "esp_http_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#endif
#include <string>
#include <vector>
#include <queue>

namespace esphome {
namespace voice_assistant_websocket {

enum VoiceAssistantWebSocketState {
  VOICE_ASSISTANT_WEBSOCKET_IDLE = 0,
  VOICE_ASSISTANT_WEBSOCKET_STARTING,
  VOICE_ASSISTANT_WEBSOCKET_RUNNING,
  VOICE_ASSISTANT_WEBSOCKET_STOPPING,
  VOICE_ASSISTANT_WEBSOCKET_ERROR,
  VOICE_ASSISTANT_WEBSOCKET_DISCONNECTED
};

class VoiceAssistantWebSocket : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_server_url(const std::string &url) { this->server_url_ = url; }
  void set_microphone(microphone::Microphone *mic) { this->microphone_ = mic; }
  void set_speaker(speaker::Speaker *spkr) { this->speaker_ = spkr; }
  
  void start();
  void stop();
  void request_start();
  void interrupt();  // Send interrupt message to server and stop speaker
  
  bool is_running() const { return this->state_ == VOICE_ASSISTANT_WEBSOCKET_RUNNING; }
  bool is_connected() const { return this->websocket_client_ != nullptr && esp_websocket_client_is_connected(this->websocket_client_); }
  bool is_bot_speaking() const;  // Check if bot is currently speaking (within 500ms of last audio)
  
  void set_state_callback(std::function<void(VoiceAssistantWebSocketState)> &&callback) {
    this->state_callback_ = std::move(callback);
  }
  
  // Automation triggers
  Trigger<> *get_connected_trigger() { return &this->connected_trigger_; }
  Trigger<> *get_disconnected_trigger() { return &this->disconnected_trigger_; }
  Trigger<> *get_error_trigger() { return &this->error_trigger_; }
  Trigger<> *get_stopped_trigger() { return &this->stopped_trigger_; }

 protected:
  void connect_websocket_();
  void disconnect_websocket_();
  void send_audio_chunk_(const uint8_t *data, size_t len);
  void process_received_audio_(const uint8_t *data, size_t len);
  void on_microphone_data_(const std::vector<uint8_t> &data);
  static void websocket_event_handler_(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data);
  void handle_websocket_event_(esp_websocket_event_id_t event_id, esp_websocket_event_data_t *event_data);
  
  std::string server_url_;
  microphone::Microphone *microphone_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  
#ifdef USE_ESP_IDF
  esp_websocket_client_handle_t websocket_client_{nullptr};
#else
  void *websocket_client_{nullptr};
#endif
  VoiceAssistantWebSocketState state_{VOICE_ASSISTANT_WEBSOCKET_IDLE};
  
  std::function<void(VoiceAssistantWebSocketState)> state_callback_;
  
  // Automation triggers
  Trigger<> connected_trigger_{};
  Trigger<> disconnected_trigger_{};
  Trigger<> error_trigger_{};
  Trigger<> stopped_trigger_{};
  
  // Audio buffers
  std::vector<uint8_t> input_buffer_;
  std::vector<uint8_t> output_buffer_;
  
  // Queue for audio data when speaker buffer is full
  // Reduced size to prevent memory exhaustion
  std::queue<std::vector<uint8_t>> audio_queue_;
  static const size_t MAX_QUEUE_SIZE = 10;  // Max 10 chunks (~40KB) to prevent memory overflow
  static const size_t MIN_FREE_HEAP_BYTES = 15000;  // Minimum free heap required before queuing audio
  
  // Timing
  uint32_t last_audio_send_{0};
  uint32_t last_audio_receive_{0};
  static const uint32_t AUDIO_SEND_INTERVAL_MS = 100;  // Send 100ms chunks
  static const uint32_t MICROPHONE_SAMPLE_RATE = 16000;  // 16kHz from microphone (required by micro_wake_word)
  static const uint32_t INPUT_SAMPLE_RATE = 24000;       // 24kHz for OpenAI input (non-beta API requirement)
  static const uint32_t OUTPUT_SAMPLE_RATE = 24000;    // 24kHz for OpenAI output
  static const uint32_t BYTES_PER_SAMPLE = 2;          // 16-bit = 2 bytes
  static const uint32_t INPUT_BUFFER_SIZE = (INPUT_SAMPLE_RATE * BYTES_PER_SAMPLE * AUDIO_SEND_INTERVAL_MS) / 1000;
  
  // Auto-stop tracking
  uint32_t last_speaker_audio_time_{0};  // Last time we received audio from speaker
  static const uint32_t AUTO_STOP_INACTIVITY_MS = 20000;  // Stop after 20 seconds of speaker inactivity
  
  // Audio conversion buffers
  std::vector<int16_t> mono_buffer_;  // For stereo to mono conversion (input)
  std::vector<int16_t> resampled_buffer_;  // For 16kHz -> 24kHz resampling (1.5x upsampling)
  std::vector<uint8_t> output_stereo_buffer_;  // For output processing (24kHz mono -> 48kHz stereo, 16-bit)
  
  bool pending_start_{false};
  bool pending_disconnect_{false};  // Flag to disconnect in loop() (cannot be called from websocket task)
  bool reconnect_pending_{false};
  bool explicit_disconnect_{false};  // Flag to prevent reconnection after explicit disconnect
  uint32_t reconnect_attempts_{0};
  static const uint32_t MAX_RECONNECT_ATTEMPTS = 5;
  static const uint32_t RECONNECT_DELAY_MS = 5000;
  uint32_t last_reconnect_attempt_{0};
  uint32_t interrupt_time_{0};  // Time when interrupt was sent (to ignore audio for a short period)
  static const uint32_t INTERRUPT_IGNORE_AUDIO_MS = 500;  // Ignore audio for 500ms after interrupt
};

// Action classes for automations (defined outside the main class)
template<typename... Ts> class VoiceAssistantWebSocketStartAction : public Action<Ts...> {
 public:
  VoiceAssistantWebSocketStartAction(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  void play(const Ts &...x) override { this->parent_->start(); }
 protected:
  VoiceAssistantWebSocket *parent_;
};

template<typename... Ts> class VoiceAssistantWebSocketStopAction : public Action<Ts...> {
 public:
  VoiceAssistantWebSocketStopAction(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  void play(const Ts &...x) override { this->parent_->stop(); }
 protected:
  VoiceAssistantWebSocket *parent_;
};

// Condition classes for automations (defined outside the main class)
template<typename... Ts> class VoiceAssistantWebSocketIsRunningCondition : public Condition<Ts...> {
 public:
  VoiceAssistantWebSocketIsRunningCondition(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->is_running(); }
 protected:
  VoiceAssistantWebSocket *parent_;
};

template<typename... Ts> class VoiceAssistantWebSocketIsConnectedCondition : public Condition<Ts...> {
 public:
  VoiceAssistantWebSocketIsConnectedCondition(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->is_connected(); }
 protected:
  VoiceAssistantWebSocket *parent_;
};

template<typename... Ts> class VoiceAssistantWebSocketIsBotSpeakingCondition : public Condition<Ts...> {
 public:
  VoiceAssistantWebSocketIsBotSpeakingCondition(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->is_bot_speaking(); }
 protected:
  VoiceAssistantWebSocket *parent_;
};

template<typename... Ts> class VoiceAssistantWebSocketInterruptAction : public Action<Ts...> {
 public:
  VoiceAssistantWebSocketInterruptAction(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  void play(const Ts &...x) override { this->parent_->interrupt(); }
 protected:
  VoiceAssistantWebSocket *parent_;
};

}  // namespace voice_assistant_websocket
}  // namespace esphome

