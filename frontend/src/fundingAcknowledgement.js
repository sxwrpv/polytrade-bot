export async function acceptFundingAndLoadAddresses({ api, version }) {
  await api.acknowledgeFunding({ accepted: true, version })
  return api.depositAddress()
}
