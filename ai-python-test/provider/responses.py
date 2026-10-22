import random

def generate_ai_response(target: str, msg: str, notif_type: str) -> str:
    """
    Generates a stochastic AI response based on the 50/10/10/10/10/10 distribution.
    """
    dice = random.random()
    
    if dice < 0.50:
        return f'{{"to": "{target}", "message": "{msg}", "type": "{notif_type}"}}'
    
    elif dice < 0.60:
        sub_dice = random.random()
        if sub_dice < 0.33:
            return f'{{"Recipient": "{target}", "body": "{msg}", "channel": "{notif_type}"}}'
        elif sub_dice < 0.66:
            return f'{{"To": "{target}", "Message": "{msg}", "Type": "{notif_type}"}}'
        else:
            return f'{{"destination": "{target}", "text": "{msg}", "method": "{notif_type}"}}'
    
    elif dice < 0.70:
        sub_dice = random.random()
        if sub_dice < 0.33: # Extra noise
            return f'{{"to": "{target}", "message": "{msg}", "type": "{notif_type}", "confidence": 0.99, "latency_ms": 120}}'
        elif sub_dice < 0.66: # Missing type
            return f'{{"to": "{target}", "message": "{msg}"}}'
        else: # Missing destination
            return f'{{"message": "{msg}", "type": "{notif_type}"}}'
    
    elif dice < 0.80:
        sub_dice = random.random()
        if sub_dice < 0.33: # Standard json block
            return f"He extraído la información correctamente:\n```json\n{{\"to\": \"{target}\", \"message\": \"{msg}\", \"type\": \"{notif_type}\"}}\n```"
        elif sub_dice < 0.66: # Just a generic code block
            return f"Output:\n```\n{{\"to\": \"{target}\", \"message\": \"{msg}\", \"type\": \"{notif_type}\"}}\n```"
        else: # Embedded in text (no blocks)
            return f"Claro, el destino es {target} y enviaré {msg} por {notif_type}. En formato JSON: {{\"to\": \"{target}\", \"message\": \"{msg}\", \"type\": \"{notif_type}\"}}"
    
    elif dice < 0.90:
        sub_dice = random.random()
        if sub_dice < 0.33: # Truncated
            return f'{{"to": "{target}", "message": "{msg}", "type": "{notif_type}" ...'
        elif sub_dice < 0.66: # Single quotes (invalid for JSON standards)
            return f"{{'to': '{target}', 'message': '{msg}', 'type': '{notif_type}'}}"
        else: # Unquoted keys
            return f'{{to: "{target}", message: "{msg}", type: "{notif_type}"}}'
    
    else:
        sub_dice = random.random()
        if sub_dice < 0.33:
            return "Lo siento, como IA no tengo permitido procesar datos de contacto personales."
        elif sub_dice < 0.66:
            return "Error: El contenido del mensaje viola las políticas de seguridad (Potential Spam)."
        else:
            return "Refused: Content analysis flagged sensitive information."
