import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: 1,
  iterations: 10,
};

const BASE_URL = 'http://provider:3001';
const HEADERS = {
  'X-API-Key': 'test-dev-2026',
  'Content-Type': 'application/json',
};

export default function () {
  const prompts = [
    "Enviar email a juan@test.com hola",
    "SMS al 600111222: cita confirmada",
    "Correo para marta@domain.es indicando que ya puede recogerlo"
  ];
  
  let aiPayload = JSON.stringify({
    messages: [
      { role: "system", content: "Extract info" },
      { role: "user", content: prompts[Math.floor(Math.random() * prompts.length)] }
    ]
  });

  let aiRes = http.post(`${BASE_URL}/v1/ai/extract`, aiPayload, { headers: HEADERS });

  check(aiRes, {
    'AI: Status is 200': (r) => r.status === 200,
    'AI: Has choices': (r) => JSON.parse(r.body).choices.length > 0,
    'AI: Message has role': (r) => JSON.parse(r.body).choices[0].message.role === 'assistant',
  });

  let notifyPayload = JSON.stringify({
    to: "test@test.com",
    message: "Test message",
    type: "email"
  });

  let notifyRes = http.post(`${BASE_URL}/v1/notify`, notifyPayload, { headers: HEADERS });

  check(notifyRes, {
    'Notify: Status is 200': (r) => r.status === 200,
    'Notify: Has provider_id': (r) => JSON.parse(r.body).provider_id !== undefined,
  });

  sleep(0.1);
}
