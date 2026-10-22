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
  const payload = JSON.stringify({
    to: "test@example.com",
    message: "Health check message",
    type: "email"
  });

  const res = http.post(`${BASE_URL}/v1/notify`, payload, { headers: HEADERS });

  check(res, {
    'Provider is responding': (r) => [200, 429, 500].includes(r.status),
    'Provider logic is active': (r) => {
      try {
        const body = JSON.parse(r.body);
        return body.provider_id !== undefined || body.error !== undefined;
      } catch (e) {
        return false;
      }
    },
  });

  sleep(0.1);
}
