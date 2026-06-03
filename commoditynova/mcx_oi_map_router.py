import { NextResponse } from "next/server";

export async function GET(
  request: Request,
  { params }: { params: { commodity: string } }
) {
  const commodity = params.commodity.toUpperCase();
  const backendUrl = process.env.NEXT_PUBLIC_API_URL || "https://greeknova-backend-production.up.railway.app";

  try {
    const res = await fetch(`${backendUrl}/mcx/oi-map/${commodity}`, {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data);
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
