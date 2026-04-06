Amazon Brand Research Wizard v3

Extension:
- Load unpacked in chrome://extensions
- Open an Amazon product page
- Save your OpenAI API key once
- Click Start Research

Architecture:
- OpenAI is used only for brand resolution, official website verification, and verified naming.
- Railway backend is the single crawler for contacts, socials, address, timezone, contact page, and B2B page.
- Browser-side crawl fallback has been removed.

Deploy backend:
- Replace main.py, requirements.txt, Procfile in Railway repo
- Redeploy service
- Verify /health and /research endpoints
