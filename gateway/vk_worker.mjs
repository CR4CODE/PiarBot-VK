import { handleVoiceRequest } from './voice_worker.mjs';
import { handleConsultRequest } from './consult_worker.mjs';

export default {
  fetch(request, env) {
    if (new URL(request.url).pathname === '/transcribe') {
      return handleVoiceRequest(request, env);
    }
    return handleConsultRequest(request, env);
  }
};
