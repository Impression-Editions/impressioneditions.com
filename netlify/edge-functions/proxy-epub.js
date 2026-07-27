export default async (request, context) => {
    const url = new URL(request.url)
    const target = url.searchParams.get('url')
    
    if (!target || !target.startsWith('https://github.com/')) {
        return new Response('Bad request', { status: 400 })
    }
    
    try {
        const response = await fetch(target)
        const headers = new Headers(response.headers)
        headers.set('Access-Control-Allow-Origin', '*')
        headers.set('Access-Control-Expose-Headers', '*')
        
        return new Response(response.body, {
            status: response.status,
            headers
        })
    } catch (e) {
        return new Response(`Proxy error: ${e.message}`, { status: 502 })
    }
}
