# Bias Python SDK

The official Python client for the [Bias API](https://biaspay.com/docs/api).

## Installation

```sh
pip install biaspay
```

## Usage

```python
from biaspay import Bias

client = Bias(api_key="sk_...")  # or set the BIAS_API_KEY environment variable

customer = client.customers.create(name="Acme", email="acme@example.com")
print(customer.id, customer.email)
```

An `AsyncBias` client mirrors the same API with awaitable methods.

## Documentation

Full reference at [biaspay.com/docs/sdks/python](https://biaspay.com/docs/sdks/python).
