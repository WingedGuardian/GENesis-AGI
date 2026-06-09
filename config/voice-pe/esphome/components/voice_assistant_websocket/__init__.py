import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.automation import maybe_simple_id
from esphome.components import microphone, speaker
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_SPEAKER
from esphome.core import CORE
from esphome.components.esp32 import add_idf_component

CODEOWNERS = ["@openai-realtime-voice-agent"]
DEPENDENCIES = ["microphone", "speaker"]

voice_assistant_websocket_ns = cg.esphome_ns.namespace("voice_assistant_websocket")
VoiceAssistantWebSocket = voice_assistant_websocket_ns.class_(
    "VoiceAssistantWebSocket", cg.Component
)

CONF_SERVER_URL = "server_url"
CONF_VOICE_ASSISTANT_WEBSOCKET = "voice_assistant_websocket"
CONF_ON_CONNECTED = "on_connected"
CONF_ON_DISCONNECTED = "on_disconnected"
CONF_ON_ERROR = "on_error"
CONF_ON_STOPPED = "on_stopped"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(VoiceAssistantWebSocket),
        cv.Required(CONF_SERVER_URL): cv.string,
        cv.Optional(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        cv.Optional(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        cv.Optional(CONF_ON_CONNECTED): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_DISCONNECTED): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_ERROR): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_STOPPED): automation.validate_automation(single=True),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    
    # Add ESP-IDF components
    if CORE.using_esp_idf:
        # WebSocket client component
        # Note: esp_websocket_client is a subdirectory in esp-protocols
        # We need to add the entire repository and reference the component path
        add_idf_component(
            name="esp-protocols",
            repo="https://github.com/espressif/esp-protocols.git",
            ref="websocket-v1.6.0",
            path="components/esp_websocket_client"
        )
    
    cg.add(var.set_server_url(config[CONF_SERVER_URL]))
    
    if CONF_MICROPHONE in config:
        mic = await cg.get_variable(config[CONF_MICROPHONE])
        cg.add(var.set_microphone(mic))
    
    if CONF_SPEAKER in config:
        spkr = await cg.get_variable(config[CONF_SPEAKER])
        cg.add(var.set_speaker(spkr))
    
    # Register automation triggers
    if CONF_ON_CONNECTED in config:
        await automation.build_automation(
            var.get_connected_trigger(), [], config[CONF_ON_CONNECTED]
        )
    
    if CONF_ON_DISCONNECTED in config:
        await automation.build_automation(
            var.get_disconnected_trigger(), [], config[CONF_ON_DISCONNECTED]
        )
    
    if CONF_ON_ERROR in config:
        await automation.build_automation(
            var.get_error_trigger(), [], config[CONF_ON_ERROR]
        )
    
    if CONF_ON_STOPPED in config:
        await automation.build_automation(
            var.get_stopped_trigger(), [], config[CONF_ON_STOPPED]
        )


# Register actions and conditions
VOICE_ASSISTANT_WEBSOCKET_ACTION_SCHEMA = maybe_simple_id(
    {
        cv.Required(CONF_ID): cv.use_id(VoiceAssistantWebSocket),
    }
)

VOICE_ASSISTANT_WEBSOCKET_CONDITION_SCHEMA = maybe_simple_id(
    {
        cv.Required(CONF_ID): cv.use_id(VoiceAssistantWebSocket),
    }
)


@automation.register_action(
    "voice_assistant_websocket.start",
    voice_assistant_websocket_ns.class_("VoiceAssistantWebSocketStartAction"),
    VOICE_ASSISTANT_WEBSOCKET_ACTION_SCHEMA,
)
async def voice_assistant_websocket_start_to_code(config, action_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, paren)


@automation.register_action(
    "voice_assistant_websocket.stop",
    voice_assistant_websocket_ns.class_("VoiceAssistantWebSocketStopAction"),
    VOICE_ASSISTANT_WEBSOCKET_ACTION_SCHEMA,
)
async def voice_assistant_websocket_stop_to_code(config, action_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, paren)


@automation.register_condition(
    "voice_assistant_websocket.is_running",
    voice_assistant_websocket_ns.class_("VoiceAssistantWebSocketIsRunningCondition"),
    VOICE_ASSISTANT_WEBSOCKET_CONDITION_SCHEMA,
)
async def voice_assistant_websocket_is_running_to_code(config, condition_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(condition_id, template_arg, paren)


@automation.register_condition(
    "voice_assistant_websocket.is_connected",
    voice_assistant_websocket_ns.class_("VoiceAssistantWebSocketIsConnectedCondition"),
    VOICE_ASSISTANT_WEBSOCKET_CONDITION_SCHEMA,
)
async def voice_assistant_websocket_is_connected_to_code(config, condition_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(condition_id, template_arg, paren)


@automation.register_action(
    "voice_assistant_websocket.interrupt",
    voice_assistant_websocket_ns.class_("VoiceAssistantWebSocketInterruptAction"),
    VOICE_ASSISTANT_WEBSOCKET_ACTION_SCHEMA,
)
async def voice_assistant_websocket_interrupt_to_code(config, action_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, paren)


@automation.register_condition(
    "voice_assistant_websocket.is_bot_speaking",
    voice_assistant_websocket_ns.class_("VoiceAssistantWebSocketIsBotSpeakingCondition"),
    VOICE_ASSISTANT_WEBSOCKET_CONDITION_SCHEMA,
)
async def voice_assistant_websocket_is_bot_speaking_to_code(config, condition_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(condition_id, template_arg, paren)

